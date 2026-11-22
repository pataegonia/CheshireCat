#!/usr/bin/env bash
# 후보 T5·T5-H2·T5-R0·G1·G2 런박스 실행 도구(11/22, 실험). 이 브랜치의 worktree(~/r5)에서 실행한다.
#
#   bash tutor/eval/t5_runbox.sh check          # 같은 키를 쓰는 다른 평가가 도는지·설정·해시
#   bash tutor/eval/t5_runbox.sh screen-tutor   # 공개 개발(교차 검증 행)·팀 세트, R·T5·T5-R0·T5-H2
#   bash tutor/eval/t5_runbox.sh screen-guard   # 공개 가드 문항, G0·G1·G2
#   bash tutor/eval/t5_runbox.sh report         # 위 두 결과 집계(본문 없음)
#   bash tutor/eval/t5_runbox.sh serve <tutor설정이름> [가드설정이름]  # 8062(튜터)·8063(가드, 줄 때만)
#   bash tutor/eval/t5_runbox.sh sub A|B        # submission.json 전환 + 요청 수 기록(공식 A/B)
#
# - 값(.env·토큰·키)은 출력하지 않는다. 비공개 채점 요청·응답 본문은 보지 않는다.
# - 홀드아웃은 쓰지 않는다(run_local은 늘 빼고, 가드 평가는 --holdout을 주지 않는다).
# - 공식 채점과 같은 키를 쓰므로 공식 제출 채점 중에는 screen-*를 돌리지 않는다(check가 막는다).
set -euo pipefail
ROOT=$(git rev-parse --show-toplevel)
cd "$ROOT"
OUT=${OUT:-$HOME/t5scr}
ENV_FILE=${ENV_FILE:-/home/elicer/competition/.env}
SUB=${SUB:-/home/elicer/competition/submission.json}
R_PORT=${R_PORT:-8052}       # 실측한 N1(40647a2)이 이미 도는 포트. 건드리지 않는다
R_COMMIT=${R_COMMIT:-40647a231aaca86949fa30887284fbbf395ac50b}
B_PORT=${B_PORT:-8062}       # 후보 튜터(serve가 띄운다)
G_PORT=${G_PORT:-8053}       # 가드. G 후보가 통과해 serve로 8063을 띄웠으면 G_PORT=8063
B_COMMIT=$(git rev-parse HEAD)
TUTOR_ARMS=(R:config/tutor_n1.yaml T5:config/tutor_t5.yaml T5R0:config/tutor_t5_r0.yaml T5H2:config/tutor_t5_h2.yaml)
GUARD_ARMS=(G0:default G1:g1 G2:g2)
mkdir -p "$OUT"

load_env() { set -a; . "$ENV_FILE"; set +a; }  # 값은 출력하지 않는다

busy() {  # 같은 키로 도는 평가·부하(이 셸 밖)가 있으면 이름만 낸다
  pgrep -af "tutor.eval.run_local|guardrail.eval.run|tutor.eval.mock_load" | grep -v "pgrep" || true
}

check() {
  echo "commit $(git rev-parse --short HEAD) dirty=[$(git status --short | wc -l)]"
  local b; b=$(busy)
  if [ -n "$b" ]; then echo "다른 평가가 돈다. 끝난 뒤 다시:"; echo "$b" | cut -c1-120; return 1; fi
  for f in config/tutor.yaml config/tutor_n1.yaml config/tutor_t5.yaml config/tutor_t5_r0.yaml \
    config/tutor_t5_h2.yaml guardrail/configs/gate128.yaml guardrail/configs/default.yaml \
    guardrail/configs/g1.yaml guardrail/configs/g2.yaml; do
    printf "%s %s\n" "$(sha256sum "$f" | cut -c1-12)" "$f"
  done
  for a in "${TUTOR_ARMS[@]}"; do
    uv run --no-sync python -c "from tutor.runtime.config import load_config; c=load_config('${a#*:}'); m=c.llm.multi_draft; print('${a%%:*}', 'drafts', [d.effort for d in m.drafts], 'hint', m.gate_hint, 'redraft', m.top1_redraft, 'oom', c.toggles.oom_gate, 'wait', m.wait_s, m.margin_s, m.gate_hint_gen_s, 'max_calls', m.max_calls, 'worst', c.worst_calls())"
  done
  for g in "${GUARD_ARMS[@]}"; do
    (cd guardrail && uv run --no-sync python -c "from guardrail.config import load_config, config_hash; from guardrail.eval.run import calls_per_row; c=load_config('configs/${g#*:}.yaml'); print('${g%%:*}', 'ensemble', c.input.ensemble, 'block_veto', c.input.block_veto, 'config_hash', config_hash(c), 'nano/guard 최대(재시도 전)', calls_per_row(c, True))")
  done
}

screen_tutor() {  # 순서 고정(결과 보기 전): 개발 R T5 T5R0 T5H2 → T5H2 T5R0 T5 R, 팀 세트 R T5 T5R0 T5H2
  check
  load_env
  export GUARD_CONFIG=guardrail/configs/gate128.yaml
  local order=(R T5 T5R0 T5H2 T5H2 T5R0 T5 R) i=0 arm cfg
  for arm in "${order[@]}"; do
    i=$((i + 1)); cfg=$(printf "%s\n" "${TUTOR_ARMS[@]}" | grep "^$arm:" | cut -d: -f2)
    echo "$(date '+%T') dev $i/${#order[@]} $arm"
    TUTOR_CONFIG=$cfg uv run --no-sync python -m tutor.eval.run_local --preset tutor --judge \
      --runs 1 --rows-jsonl "$OUT/dev_${arm}_$i.jsonl" --out "$OUT/runs" > "$OUT/dev_${arm}_$i.log" 2>&1 \
      || echo "  실패 rc=$? (로그 $OUT/dev_${arm}_$i.log)"
    tail -n 3 "$OUT/dev_${arm}_$i.log"
  done
  for arm in R T5 T5R0 T5H2; do
    cfg=$(printf "%s\n" "${TUTOR_ARMS[@]}" | grep "^$arm:" | cut -d: -f2)
    echo "$(date '+%T') team $arm"
    TUTOR_CONFIG=$cfg uv run --no-sync python -m tutor.eval.run_local --preset tutor --judge \
      --cases team --runs 1 --rows-jsonl "$OUT/team_$arm.jsonl" --out "$OUT/runs" > "$OUT/team_$arm.log" 2>&1 \
      || echo "  실패 rc=$? (로그 $OUT/team_$arm.log)"
    tail -n 3 "$OUT/team_$arm.log"
  done
}

screen_guard() {  # 순서 고정: G0 G1 G2 → G2 G1 G0, 반복 1씩(가드 하네스가 반복마다 표를 낸다)
  check
  load_env
  local order=(G0 G1 G2 G2 G1 G0) i=0 arm g
  for arm in "${order[@]}"; do
    i=$((i + 1)); g=$(printf "%s\n" "${GUARD_ARMS[@]}" | grep "^$arm:" | cut -d: -f2)
    echo "$(date '+%T') guard $i/${#order[@]} $arm"
    (cd guardrail && env -u GUARD_CONFIG uv run --no-sync python -m guardrail.eval.run \
      --config "configs/$g.yaml" --name "${arm}_$i" --repeat 1 --out "$OUT/guard" \
      > "$OUT/guard_${arm}_$i.log" 2>&1) || echo "  실패 rc=$? (로그 $OUT/guard_${arm}_$i.log)"
  done
}

report() {
  local args=() f
  for f in "$OUT"/dev_*_*.jsonl; do [ -e "$f" ] && args+=("$(basename "$f" | cut -d_ -f2)=$f"); done
  echo "## 공개 개발(교차 검증 행)"
  [ ${#args[@]} -gt 0 ] && uv run --no-sync python -m tutor.eval.five_draft_report "${args[@]}" --base R
  args=()
  for f in "$OUT"/team_*.jsonl; do [ -e "$f" ] && args+=("$(basename "$f" .jsonl | cut -d_ -f2)=$f"); done
  echo; echo "## 팀 점검 세트"
  [ ${#args[@]} -gt 0 ] && uv run --no-sync python -m tutor.eval.five_draft_report "${args[@]}" --base R
  echo; echo "## run_local 요약(로그 끝, 범주 가중 튜터 점수 포함)"
  for f in "$OUT"/dev_*.log "$OUT"/team_*.log; do
    [ -e "$f" ] || continue
    echo "### $(basename "$f")"; grep -E "튜터|점수|시간 초과|형식|실패|429|p95" "$f" | tail -n 8 | cut -c1-300
  done
  echo; echo "## 가드(반복마다 보고서 표 줄·요약)"
  for f in "$OUT"/guard/*/report.md; do
    [ -e "$f" ] || continue
    grep -E "^\| (G0|G1|G2)_[0-9]+ \||출력 평균|호출 [0-9]+회|지연 p50|혼동행렬|판정기 오류" "$f" | cut -c1-400
  done
}

serve() {  # 후보 튜터 8062(+ 가드 8063). 이미 도는 8051·8052·8053과 다른 서버는 건드리지 않는다
  local tcfg=${1:?튜터 설정 이름(예 t5)} gcfg=${2:-}
  load_env
  unset TUTOR_CONFIG GUARD_CONFIG
  setsid nohup env GUARD_CONFIG=guardrail/configs/gate128.yaml TUTOR_CONFIG="config/tutor_$tcfg.yaml" \
    uv run --no-sync uvicorn app:app --host 0.0.0.0 --port "$B_PORT" --no-access-log \
    > "$HOME/server-$B_PORT.log" 2>&1 < /dev/null &
  if [ -n "$gcfg" ]; then
    setsid nohup env GUARD_CONFIG="guardrail/configs/$gcfg.yaml" \
      uv run --no-sync uvicorn app:app --host 0.0.0.0 --port 8063 --no-access-log \
      > "$HOME/server-8063.log" 2>&1 < /dev/null &
  fi
  echo "띄움: $B_PORT(tutor_$tcfg)${gcfg:+ · 8063($gcfg)}. 로그에서 Application startup complete 확인"
}

counts() {  # 요청 수·초안 상태(코드만)
  local p
  for p in "$R_PORT" "$B_PORT" 8051 "$G_PORT"; do
    [ -e "$HOME/server-$p.log" ] || continue
    printf "%s tutor=%s guard=%s\n" "$p" "$(grep -c ' /tutor ' "$HOME/server-$p.log")" \
      "$(grep -c ' /guard ' "$HOME/server-$p.log")"
  done
  for p in "$R_PORT" "$B_PORT"; do
    [ -e "$HOME/server-$p.log" ] || continue
    # 초안 상태 v/f/l 합, 고른 초안 번호 분포, top1 재작성 skip_time, 요청당 튜터 시도 수(calls=) 분포
    printf "%s drafts %s| chosen %s| skip_time=%s | calls %s\n" "$p" \
      "$(grep -o 'multi_draft drafts=[vfl]*' "$HOME/server-$p.log" | cut -d= -f2 | fold -w1 | sort | uniq -c | awk '{printf "%s=%s ", $2, $1}')" \
      "$(grep -o 'multi_draft chosen=[0-9a-z]*' "$HOME/server-$p.log" | cut -d= -f2 | sort | uniq -c | awk '{printf "%s=%s ", $2, $1}')" \
      "$(grep -c 'skip_time' "$HOME/server-$p.log")" \
      "$(grep -o 'calls=[0-9]*' "$HOME/server-$p.log" | cut -d= -f2 | sort -n | uniq -c | awk '{printf "%s=%s ", $2, $1}')"
    printf "%s hint %s\n" "$p" "$(grep -o 'multi_draft hint=[a-z_]*' "$HOME/server-$p.log" | cut -d= -f2 | sort | uniq -c | awk '{printf "%s=%s ", $2, $1}')"
  done
}

sub() {  # A = R(8052, 40647a2), B = 후보(8062, 이 커밋). 가드는 G_PORT
  local arm=${1:?A 또는 B} port commit
  case "$arm" in A) port=$R_PORT commit=$R_COMMIT ;; B) port=$B_PORT commit=$B_COMMIT ;; *) echo "A|B"; return 1 ;; esac
  if [ "$port" = 8051 ]; then echo "8051(기준선 M)은 제출하지 않는다"; return 1; fi
  python3 - "$SUB" "$port" "$G_PORT" "$commit" <<'PY'
import json, re, sys
path, port, guard, commit = sys.argv[1:]
data = json.load(open(path, encoding="utf-8"))
data["tutor_url"] = re.sub(r"/proxy/\d+/", f"/proxy/{port}/", data["tutor_url"])
data["guard_url"] = re.sub(r"/proxy/\d+/", f"/proxy/{guard}/", data["guard_url"])
data["commit"] = commit
json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False)
print("tutor", re.search(r"/proxy/(\d+)/", data["tutor_url"]).group(1),
      "guard", re.search(r"/proxy/(\d+)/", data["guard_url"]).group(1), "commit", commit[:7])
PY
  echo "$(date '+%F %T') $arm port=$port guard=$G_PORT commit=${commit:0:7}" >> "$HOME/t5_order.txt"
  counts
}

case "${1:-}" in
  check) check ;;
  screen-tutor) screen_tutor ;;
  screen-guard) screen_guard ;;
  report) report ;;
  serve) shift; serve "$@" ;;
  sub) shift; sub "$@" ;;
  counts) counts ;;
  *) sed -n '2,14p' "$0"; exit 1 ;;
esac
