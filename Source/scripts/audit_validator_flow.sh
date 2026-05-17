#!/bin/bash
# Watch flight_recorder for validator/trade events and emit one line per event.
# Used by claude Monitor to surface end-to-end pipeline activity for iter 20d audit.

DB="<repo_root>/Source/flight_recorder.db"

# Track high-water-mark of seen flight_log ids
last_id=$(sqlite3 "$DB" "SELECT COALESCE(MAX(id), 0) FROM flight_log")
echo "[audit] starting watch from flight_log id > $last_id"

while true; do
    # Pull new events for the stages we care about
    rows=$(sqlite3 -separator '|' "$DB" "
        SELECT
            id,
            substr(datetime(timestamp,'localtime'),12,8) AS tm,
            pair,
            stage,
            COALESCE(trade_id,'') AS tid,
            json_extract(data,'\$.verdict') AS verdict,
            json_extract(data,'\$.confidence') AS conf,
            json_extract(data,'\$.direction') AS dir,
            json_extract(data,'\$.has_patterns') AS has_pat,
            json_extract(data,'\$.has_scout') AS has_sc,
            json_extract(data,'\$.data_sections') AS sect,
            COALESCE(substr(note,1,80),'') AS note
        FROM flight_log
        WHERE id > $last_id
          AND stage IN ('validator_call','validator_verdict','trade_phase','trade_close','SNIPE_GATE_PASSED','SNIPE_GATE_BLOCKED','guardian_action')
        ORDER BY id
    ")

    if [ -n "$rows" ]; then
        while IFS='|' read -r id tm pair stage tid verdict conf direction has_pat has_sc sect note; do
            case "$stage" in
                validator_call)
                    # Only emit when sections delivered (skip empty fast-path log entries)
                    if [ -n "$sect" ] && [ "$sect" != "" ]; then
                        echo "[$tm] CALL    $pair  sections=$sect  patterns=${has_pat:-?}  scout=${has_sc:-?}"
                    fi
                    ;;
                validator_verdict)
                    # Show only confirm/skip (not noise WATCH for verbose)
                    if [ "$verdict" = "CONFIRM" ] || [ "$verdict" = "SKIP" ]; then
                        echo "[$tm] VERDICT $pair  $verdict  $direction  conf=$conf"
                    fi
                    ;;
                trade_phase)
                    echo "[$tm] PHASE   $pair  trade=$tid  $note"
                    ;;
                trade_close)
                    echo "[$tm] CLOSE   $pair  trade=$tid  $note"
                    ;;
                SNIPE_GATE_PASSED|SNIPE_GATE_BLOCKED)
                    echo "[$tm] $stage  $pair  $note"
                    ;;
                guardian_action)
                    # Skip pure trailing-stop ticks (every minute is noise). Surface
                    # only state-change events: locks, cuts, profit-floor, BE move,
                    # exits, threats, errors.
                    case "$note" in
                        *"Dynamic SL: E"*) ;;  # routine trailing — skip
                        *) echo "[$tm] GUARDIAN $pair  trade=$tid  $note" ;;
                    esac
                    ;;
            esac
            last_id=$id
        done <<< "$rows"
    fi

    sleep 8
done
