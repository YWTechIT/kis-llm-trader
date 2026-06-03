"""vendor/kis 모듈을 안전한 순서로 로드하는 단일 진입점.

공식 `kis_auth.py`는 import 시점에 `~/KIS/config/kis_devlp.yaml`을 읽으므로,
반드시 `config`(yaml 생성)를 먼저 import해야 한다. 이 모듈이 그 순서를 강제한다.

사용:
    from kis_bootstrap import ka, kb, kws, settings
"""

from __future__ import annotations

import os
import sys

# 1) config가 .env 검증 + kis_devlp.yaml 생성을 import 시점에 수행한다.
import config  # noqa: F401  (side-effect import — 순서 중요)

settings = config.settings

# 2) vendor/kis 를 import 경로에 추가(서로 `import kis_auth as ka` 로 참조).
_VENDOR_KIS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "kis")
if _VENDOR_KIS not in sys.path:
    sys.path.insert(0, _VENDOR_KIS)

# 3) 이제 공식 모듈을 로드한다(이 시점에 yaml이 존재해야 함).
import kis_auth as ka  # noqa: E402
import domestic_stock_functions as kb  # noqa: E402
import domestic_stock_functions_ws as kws  # noqa: E402

__all__ = ["ka", "kb", "kws", "settings"]
