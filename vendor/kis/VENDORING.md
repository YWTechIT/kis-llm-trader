# vendor/kis — 공식 KIS 샘플 복사본

이 폴더의 파일들은 공식 레포 `koreainvestment/open-trading-api`에서 **수정 없이 그대로 복사**한 것입니다.
앱은 `reference/`(submodule)를 직접 import하지 않고 이 사본만 import합니다.

| 파일 | 원본 경로 (examples_user/) |
|------|----------------------------|
| `kis_auth.py` | `kis_auth.py` |
| `domestic_stock_functions.py` | `domestic_stock/domestic_stock_functions.py` |
| `domestic_stock_functions_ws.py` | `domestic_stock/domestic_stock_functions_ws.py` |

- **복사 시점 submodule 커밋:** `33e0e1e65cd1c8c8b639531483ec0b327087bab1`
- **수정 금지:** 파일을 직접 고치지 않습니다(업데이트 추적 용이). 동작 차이는 `adapter/` 래퍼에서 흡수합니다.
- `kis_auth.py`는 import 시점에 `~/KIS/config/kis_devlp.yaml`을 읽고 토큰 캐시 파일을 생성합니다.
  이 yaml은 `config.py`가 `.env` 값으로 런타임 생성합니다(절대 커밋되지 않음).

## 업데이트 방법
```
git -C reference/open-trading-api pull
cp reference/open-trading-api/examples_user/kis_auth.py vendor/kis/
cp reference/open-trading-api/examples_user/domestic_stock/domestic_stock_functions.py vendor/kis/
cp reference/open-trading-api/examples_user/domestic_stock/domestic_stock_functions_ws.py vendor/kis/
```
복사 후 위 커밋 해시를 갱신하세요.
