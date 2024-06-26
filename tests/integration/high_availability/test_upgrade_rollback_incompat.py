# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

import ast
import logging
import os
import pathlib
import shutil
import subprocess
from time import sleep
from zipfile import ZipFile

import pytest
from pytest_operator.plugin import OpsTest

from .. import juju_
from ..helpers import get_leader_unit, get_relation_data, get_unit_by_index
from .high_availability_helpers import (
    ensure_all_units_continuous_writes_incrementing,
    relate_mysql_and_application,
)

logger = logging.getLogger(__name__)

TIMEOUT = 20 * 60
MYSQL_APP_NAME = "mysql"
TEST_APP = "mysql-test-app"


@pytest.mark.group(1)
@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test: OpsTest, mysql_charm_series: str) -> None:
    """Simple test to ensure that the mysql and application charms get deployed."""
    sub_regex_older_snap = "s/CHARMED_MYSQL_SNAP_REVISION.*/CHARMED_MYSQL_SNAP_REVISION = 69/"
    src_patch(sub_regex=sub_regex_older_snap, file_name="src/constants.py")
    charm = await charm_local_build(ops_test)

    src_patch(revert=True)
    config = {"profile": "testing"}

    async with ops_test.fast_forward("10s"):
        await ops_test.model.deploy(
            charm,
            application_name=MYSQL_APP_NAME,
            config=config,
            num_units=3,
            series=mysql_charm_series,
        )

        await ops_test.model.deploy(
            TEST_APP,
            application_name=TEST_APP,
            channel="latest/edge",
            num_units=1,
        )

        await relate_mysql_and_application(ops_test, MYSQL_APP_NAME, TEST_APP)
        await ops_test.model.wait_for_idle(
            apps=[MYSQL_APP_NAME, TEST_APP],
            status="active",
            timeout=TIMEOUT,
        )


@pytest.mark.group(1)
@pytest.mark.abort_on_fail
async def test_pre_upgrade_check(ops_test: OpsTest) -> None:
    """Test that the pre-upgrade-check action runs successfully."""
    logger.info("Get leader unit")
    leader_unit = await get_leader_unit(ops_test, MYSQL_APP_NAME)

    assert leader_unit is not None, "No leader unit found"
    logger.info("Run pre-upgrade-check action")
    await juju_.run_action(leader_unit, "pre-upgrade-check")


@pytest.mark.group(1)
@pytest.mark.abort_on_fail
async def test_upgrade_to_failling(
    ops_test: OpsTest,
    continuous_writes,
) -> None:
    logger.info("Ensure continuous_writes")
    await ensure_all_units_continuous_writes_incrementing(ops_test)

    application = ops_test.model.applications[MYSQL_APP_NAME]
    logger.info("Build charm locally")

    sub_regex_failing_rejoin = (
        's/logger.debug("Recovering unit")'
        "/self.charm._mysql.set_instance_offline_mode(True); raise RetryError/"
    )
    src_patch(sub_regex=sub_regex_failing_rejoin, file_name="src/upgrade.py")
    new_charm = await charm_local_build(ops_test, refresh=True)
    src_patch(revert=True)

    logger.info("Refresh the charm")
    await application.refresh(path=new_charm)

    logger.info("Wait for upgrade to start")
    await ops_test.model.block_until(
        lambda: "waiting" in {unit.workload_status for unit in application.units},
        timeout=TIMEOUT,
    )
    logger.info("Get first upgrading unit")
    relation_data = await get_relation_data(ops_test, MYSQL_APP_NAME, "upgrade")
    upgrade_stack = relation_data[0]["application-data"]["upgrade-stack"]
    upgrading_unit = get_unit_by_index(
        MYSQL_APP_NAME, application.units, ast.literal_eval(upgrade_stack)[-1]
    )

    assert upgrading_unit is not None, "No upgrading unit found"

    logger.info("Wait for upgrade to fail on upgrading unit")
    await ops_test.model.block_until(
        lambda: upgrading_unit.workload_status == "blocked",
        timeout=TIMEOUT,
    )


@pytest.mark.group(1)
@pytest.mark.abort_on_fail
@pytest.mark.unstable
async def test_rollback(ops_test, continuous_writes) -> None:
    application = ops_test.model.applications[MYSQL_APP_NAME]

    sub_regex_older_snap = "s/CHARMED_MYSQL_SNAP_REVISION.*/CHARMED_MYSQL_SNAP_REVISION = 69/"
    src_patch(sub_regex=sub_regex_older_snap, file_name="src/constants.py")
    charm = await charm_local_build(ops_test, refresh=True)

    logger.info("Get leader unit")
    leader_unit = await get_leader_unit(ops_test, MYSQL_APP_NAME)

    assert leader_unit is not None, "No leader unit found"

    sleep(10)
    logger.info("Run pre-upgrade-check action")
    await juju_.run_action(leader_unit, "pre-upgrade-check")

    sleep(20)
    logger.info("Refresh with previous charm")
    await application.refresh(path=charm)

    logger.info("Wait for upgrade to start")
    await ops_test.model.block_until(
        lambda: "waiting" in {unit.workload_status for unit in application.units},
        timeout=TIMEOUT,
    )
    await ops_test.model.wait_for_idle(apps=[MYSQL_APP_NAME], status="active", timeout=TIMEOUT)

    logger.info("Ensure continuous_writes after rollback procedure")
    await ensure_all_units_continuous_writes_incrementing(ops_test)


def src_patch(sub_regex: str = "", file_name: str = "", revert: bool = False) -> None:
    """Apply a patch to the source code."""
    if revert:
        cmd = "git checkout src/"  # revert changes on src/ dir
        logger.info("Reverting patch on source")
    else:
        cmd = f"sed -i -e '{sub_regex}' {file_name}"
        logger.info("Applying patch to source")
    subprocess.run([cmd], shell=True, check=True)


async def charm_local_build(ops_test: OpsTest, refresh: bool = False):
    """Wrapper for a local charm build zip file updating."""
    local_charms = pathlib.Path().glob("local-*.charm")
    for lc in local_charms:
        # clean up local charms from previous runs to avoid
        # pytest_operator_cache globbing them
        lc.unlink()

    charm = await ops_test.build_charm(".")

    if os.environ.get("CI") == "true":
        # CI will get charm from common cache
        # make local copy and update charm zip

        update_files = ["src/constants.py", "src/upgrade.py"]

        charm = pathlib.Path(shutil.copy(charm, f"local-{charm.stem}.charm"))

        for path in update_files:
            with open(path, "r") as f:
                content = f.read()

            with ZipFile(charm, mode="a") as charm_zip:
                charm_zip.writestr(path, content)

    if refresh:
        # when refreshing, return posix path
        return charm
    # when deploying, return prefixed full path
    return f"local:{charm.resolve()}"
