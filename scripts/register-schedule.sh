#!/usr/bin/env bash
# register-schedule.sh — 幂等地把一个定时任务登记进 AWS EventBridge Scheduler
#
# 默认 DRY_RUN=1：只打印将要执行的命令，不做任何变更。
# 真正执行前必须显式 export DRY_RUN=0。
#
# 用法：
#   ./register-schedule.sh \
#     --name cctv-prune-daily \
#     --expression 'cron(30 4 * * ? *)' \
#     --timezone Asia/Shanghai \
#     --source custom.cctv --detail-type prune-daily \
#     --input '{"action":"prune-daily"}'
#
# 说明：
#   - 本脚本只建/更新 Schedule（时钟层）。Rule + API Destination 属于路由层，
#     见 docs/RUNBOOK-add-a-job.md Step 3。
#   - Schedule 的 target 固定为 default event bus，源/类型由 --source/--detail-type 决定，
#     事件里带 --input 的 JSON 作为 Input，Rule 侧再翻译成别的东西。

set -euo pipefail

REGION="${AWS_REGION:-ap-southeast-1}"
GROUP="${GROUP:-default}"
ROLE_ARN="${ROLE_ARN:-arn:aws:iam::186004631963:role/eventbridge-scheduler-exec-role}"
DRY_RUN="${DRY_RUN:-1}"

NAME="" EXPRESSION="" TZ="" SOURCE="" DETAIL_TYPE="" INPUT="" DESCRIPTION=""

die() { echo "ERROR: $*" >&2; exit 1; }
usage() { grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)        NAME="$2"; shift 2 ;;
    --expression)  EXPRESSION="$2"; shift 2 ;;
    --timezone)    TZ="$2"; shift 2 ;;
    --source)      SOURCE="$2"; shift 2 ;;
    --detail-type) DETAIL_TYPE="$2"; shift 2 ;;
    --input)       INPUT="$2"; shift 2 ;;
    --description) DESCRIPTION="$2"; shift 2 ;;
    -h|--help)     usage ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ -n "$NAME" && -n "$EXPRESSION" && -n "$SOURCE" && -n "$DETAIL_TYPE" ]] \
  || die "--name --expression --source --detail-type are required"
[[ -n "$TZ" ]] || die "--timezone is required (do not rely on the UTC default)"
[[ -n "$INPUT" ]] || INPUT='{"action":"run"}'

echo "== region=$REGION group=$GROUP role=$ROLE_ARN"
echo "== name=$NAME  expr=$EXPRESSION  tz=$TZ"
echo "== source=$SOURCE  detail-type=$DETAIL_TYPE"
echo "== input=$INPUT"
echo

# 幂等判定：存在则 update，不存在则 create
if aws scheduler get-schedule --region "$REGION" --name "$NAME" --group-name "$GROUP" >/dev/null 2>&1; then
  ACTION="update-schedule"
  echo "== schedule exists -> will UPDATE"
else
  ACTION="create-schedule"
  echo "== schedule not found -> will CREATE"
fi

CMD=(aws scheduler "$ACTION" \
  --region "$REGION" \
  --name "$NAME" \
  --group-name "$GROUP" \
  --schedule-expression "$EXPRESSION" \
  --schedule-expression-timezone "$TZ" \
  --flexible-time-window '{"Mode":"OFF"}' \
  --state ENABLED \
  --target "{\"Arn\":\"arn:aws:events:${REGION}:186004631963:event-bus/${GROUP}\",\"RoleArn\":\"${ROLE_ARN}\",\"Input\":$(printf '%s' "$INPUT" | jq -Rs .),\"EventBridgeParameters\":{\"Source\":\"${SOURCE}\",\"DetailType\":\"${DETAIL_TYPE}\"},\"RetryPolicy\":{\"MaximumRetryAttempts\":185,\"MaximumEventAgeInSeconds\":86400}}")

[[ -n "$DESCRIPTION" ]] && CMD+=(--description "$DESCRIPTION")

if [[ "$DRY_RUN" == "1" ]]; then
  echo "== DRY RUN, would execute:"
  printf '   %q ' "${CMD[@]}"; echo
  echo
  echo "== re-run with DRY_RUN=0 to apply"
  exit 0
fi

"${CMD[@]}"
echo
echo "== applied. verify with:"
echo "   aws scheduler get-schedule --region $REGION --name $NAME --group-name $GROUP"
echo "== remember to update docs/INVENTORY.md"
