from pathlib import Path

import pytest

from price_alert.instance import AlreadyRunningError, ProcessLock


def test_process_lock_rejects_second_instance_and_can_be_reused(tmp_path: Path):
    path = tmp_path / "monitor.lock"

    with ProcessLock(path):
        with pytest.raises(AlreadyRunningError, match="已经在运行"):
            with ProcessLock(path):
                pass

    # 正常退出必须释放锁，否则一次停止后将永远无法再次启动。
    with ProcessLock(path):
        pass
