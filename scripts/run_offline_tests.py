"""离线运行 pytest：不读取项目真实 .env、不启用真实 API 测试、阻断 HTTP。"""

import os
from pathlib import Path
import sys
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from src.llm_provider import LLMConfig


def main():
    import pytest

    os.environ.pop("DATAPILOT_LIVE_API", None)
    original = LLMConfig.from_environment.__func__

    def offline_config(cls, env_file=None):
        if env_file is None or Path(env_file).resolve() == PROJECT / ".env":
            return None
        # 配置单测仅允许继续读取明确传入的虚构临时文件或空白模板。
        return original(cls, env_file)

    with patch.object(LLMConfig, "from_environment", classmethod(offline_config)), \
            patch("requests.sessions.Session.request", side_effect=AssertionError("离线测试禁止HTTP")):
        return pytest.main(sys.argv[1:] or ["-q"])


if __name__ == "__main__":
    raise SystemExit(main())
