#!/usr/bin/env bash

set -u

VHCLIENT="/opt/virtualhere/vhclientarm64"

if [[ ! -x "$VHCLIENT" ]]; then
    echo "VirtualHere client not found or not executable: $VHCLIENT"
    exit 1
fi

declare -a ALL_LABELS
declare -a ALL_ADDRS
declare -a ALL_SUFFIXES
declare -a USED_LABELS
declare -a USED_ADDRS
declare -a USED_SUFFIXES

run_vh() {
    "$VHCLIENT" -t "$1" 2>&1
}

trim() {
    local s="$1"
    s="${s#"${s%%[![:space:]]*}"}"
    s="${s%"${s##*[![:space:]]}"}"
    printf '%s' "$s"
}

load_devices() {
    ALL_LABELS=()
    ALL_ADDRS=()
    ALL_SUFFIXES=()
    USED_LABELS=()
    USED_ADDRS=()
    USED_SUFFIXES=()

    local raw line current_hub=""
    raw="$(run_vh "LIST")"

    if [[ "$raw" == ERROR:* ]]; then
        echo "$raw"
        echo
        echo "The VirtualHere client is not responding."
        return 1
    fi

    while IFS= read -r line; do
        # Hub line, e.g.:
        # OpenWRT Hub (GL-BE3600:7575)
        if [[ "$line" != *"-->"* ]] && [[ "$line" =~ ^[[:space:]]*(.+)[[:space:]]+\(([^()]*)[:]([0-9]+)\)[[:space:]]*$ ]]; then
            current_hub="$(trim "${BASH_REMATCH[1]}")"
            continue
        fi

        # Device line, e.g.:
        #   --> ZUOYA GMK26 (GL-BE3600.1134) (In-use by you)
        if [[ "$line" == *"-->"* ]]; then
            local rest name addr status label suffix auto_use="0"

            rest="${line#*-->}"
            rest="$(trim "$rest")"

            status=""
            if [[ "$rest" =~ ^(.*)[[:space:]]+\(([^()]*)\)[[:space:]]+\(([^()]*)\)[[:space:]]*$ ]]; then
                name="$(trim "${BASH_REMATCH[1]}")"
                addr="$(trim "${BASH_REMATCH[2]}")"
                status="$(trim "${BASH_REMATCH[3]}")"
            elif [[ "$rest" =~ ^(.*)[[:space:]]+\(([^()]*)\)[[:space:]]*$ ]]; then
                name="$(trim "${BASH_REMATCH[1]}")"
                addr="$(trim "${BASH_REMATCH[2]}")"
            else
                continue
            fi

            if [[ "$name" == \** ]]; then
                auto_use="1"
                name="$(trim "${name#\*}")"
            fi

            label="${current_hub} -> ${name}"
            suffix=""
            if [[ "$auto_use" == "1" ]]; then
                suffix="Auto-use"
            fi
            if [[ -n "$status" ]]; then
                if [[ -n "$suffix" ]]; then
                    suffix="${suffix}, ${status}"
                else
                    suffix="$status"
                fi
            fi

            ALL_LABELS+=("$label")
            ALL_ADDRS+=("$addr")
            ALL_SUFFIXES+=("$suffix")

            if [[ "$status" == "In-use by you" ]]; then
                USED_LABELS+=("$label")
                USED_ADDRS+=("$addr")
                USED_SUFFIXES+=("$suffix")
            fi
        fi
    done <<< "$raw"

    return 0
}

show_numbered_list() {
    local -n labels_ref=$1
    local -n addrs_ref=$2
    local -n suffixes_ref=$3
    local title="$4"

    echo
    echo "$title"

    if [[ ${#labels_ref[@]} -eq 0 ]]; then
        echo "  none"
        echo
        return 1
    fi

    local i
    for ((i=0; i<${#labels_ref[@]}; i++)); do
        if [[ -n "${suffixes_ref[$i]}" ]]; then
            printf "  [%d] %s [%s] (%s)\n" "$((i+1))" "${labels_ref[$i]}" "${addrs_ref[$i]}" "${suffixes_ref[$i]}"
        else
            printf "  [%d] %s [%s]\n" "$((i+1))" "${labels_ref[$i]}" "${addrs_ref[$i]}"
        fi
    done
    echo
    return 0
}

pick_device() {
    local -n labels_ref=$1
    local -n addrs_ref=$2

    local choice
    read -rp "Choose device number: " choice

    if ! [[ "$choice" =~ ^[0-9]+$ ]]; then
        echo "Invalid device number."
        return 1
    fi

    if (( choice < 1 || choice > ${#addrs_ref[@]} )); then
        echo "Selection out of range."
        return 1
    fi

    PICKED_LABEL="${labels_ref[$((choice - 1))]}"
    PICKED_ADDR="${addrs_ref[$((choice - 1))]}"
    return 0
}

address_is_used_by_you() {
    local addr="$1"
    local used_addr
    for used_addr in "${USED_ADDRS[@]}"; do
        if [[ "$used_addr" == "$addr" ]]; then
            return 0
        fi
    done
    return 1
}

confirm_command_result() {
    local addr="$1"
    local expected_state="$2"

    sleep 1
    if ! load_devices >/dev/null 2>&1; then
        return 1
    fi

    if [[ "$expected_state" == "mounted" ]]; then
        address_is_used_by_you "$addr"
        return $?
    fi

    if address_is_used_by_you "$addr"; then
        return 1
    fi
    return 0
}

do_add() {
    local addr="$1"
    local result
    result="$(run_vh "USE,${addr}")"
    if [[ "$(trim "$result")" == "FAILED" ]] && confirm_command_result "$addr" "mounted"; then
        echo "OK (VirtualHere timed out, but the device is now mounted.)"
        return 0
    fi
    echo "$result"
}

do_remove() {
    local addr="$1"
    local result
    result="$(run_vh "STOP USING,${addr}")"
    if [[ "$(trim "$result")" == "FAILED" ]] && confirm_command_result "$addr" "unmounted"; then
        echo "OK (VirtualHere timed out, but the device is now unmounted.)"
        return 0
    fi
    echo "$result"
}

main_loop() {
    while true; do
        if ! load_devices; then
            exit 1
        fi

        show_numbered_list ALL_LABELS ALL_ADDRS ALL_SUFFIXES "Available VirtualHere devices:"
        echo "[A]dd  [R]emove  [E]xit"
        echo

        local action
        read -rp "Choose action: " action
        action="${action^^}"

        case "$action" in
            A)
                if [[ ${#ALL_ADDRS[@]} -eq 0 ]]; then
                    echo "No devices available."
                    echo
                    continue
                fi

                if ! pick_device ALL_LABELS ALL_ADDRS; then
                    echo
                    continue
                fi

                echo
                echo "Adding: $PICKED_LABEL [$PICKED_ADDR]"
                do_add "$PICKED_ADDR"
                echo
                ;;
            R)
                if ! show_numbered_list USED_LABELS USED_ADDRS USED_SUFFIXES "Devices currently in use by this client:"; then
                    echo "Nothing to remove."
                    echo
                    continue
                fi

                if ! pick_device USED_LABELS USED_ADDRS; then
                    echo
                    continue
                fi

                echo
                echo "Removing: $PICKED_LABEL [$PICKED_ADDR]"
                do_remove "$PICKED_ADDR"
                echo
                ;;
            E)
                exit 0
                ;;
            *)
                echo "Invalid action."
                echo
                ;;
        esac
    done
}

main_loop
