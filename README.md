uv run python ci_push.py \
  --repo SNOWFLAKE_LEARNING_DB.DATA_TEST.DOC_TOOLS \
  --image lo-convert --tag latest \
  --build \
  --put-spec --spec-path specs/lo_convert.yaml \
  --stage @DOC_STAGE --spec-dest specs/lo_convert.yaml \
  --connection XVRALMC-UX32060



使い方
1. XS コンピュートプールを作る
uv run python ci_spcs.py pool \
  --name LO_POOL_XS \
  --size XS \
  --min-nodes 1 --max-nodes 1 \
  --connection XVRALMC-UX32060

2. Job Service を投げる
uv run python ci_spcs.py job \
  --pool LO_POOL_XS \
  --stage @DOC_STAGE \
  --spec specs/lo_convert.yaml \
  --name DOC_CONVERT \
  --replicas 2 \
  --connection XVRALMC-UX32060


（同期で待ちたいなら --sync を追加）

3. ログを見る
uv run python ci_spcs.py logs \
  --name DOC_CONVERT \
  --container lo \
  --connection XVRALMC-UX32060