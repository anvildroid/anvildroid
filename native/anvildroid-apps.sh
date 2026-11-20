#!/system/bin/sh
# Narrow app-management queue shared with the logged-in host user (Android
# system group), never an arbitrary command interpreter. No eval/shell payloads.
umask 007
base=/data/waydroid_tmp/anvildroid
cd "$base" || exit 1
mkdir -p apps
chmod 2770 apps
# Use mksh's timeout instead of creating a sleep process on every idle tick.
tick_ready=0
if [ -n "${KSH_VERSION:-}" ]; then
    tick_dir=$(mktemp -d /data/local/tmp/anvil-app-tick-XXXXXX)
    if [ -n "$tick_dir" ] && mkfifo "$tick_dir/tick" && { exec 9<>"$tick_dir/tick"; }; then
        tick_ready=1
    fi
    # The open descriptor holds the FIFO alive; no pathname or exit trap is
    # needed while processing queue jobs in subshells.
    if [ -n "$tick_dir" ]; then rm -f "$tick_dir/tick"; rmdir "$tick_dir"; fi
fi
wait_tick() {
    if [ "$tick_ready" -eq 1 ]; then IFS= read -r -t "$1" tick <&9 || :
    else sleep "$1"; fi
}
while true; do
    for request in apps/job-*.ready; do
        [ -d "$request" ] && [ ! -L "$request" ] || continue
        work=${request%.ready}.working
        mv "$request" "$work" || continue
        (
            cd "$work" || exit 1
            exec > output.tmp 2>&1
            read -r action < action
            read -r package < package
            case "$package" in
                ''|*[!a-zA-Z0-9_.]*|.*|*.|*..*) echo 'Invalid package'; exit 2;;
            esac
            case "$action" in
                details) dumpsys package "$package" ;;
                installs)
                    # Asked before an install is queued: does the package service
                    # answer, and which install sessions is Android working on.
                    pm path android
                    dumpsys package installs | head -c 262144 ;;
                install)
                    if [ -e count ] || [ -L count ] || [ -e input-0.apk ] || [ -L input-0.apk ]; then
                        # Split package: the host writes how many APKs it staged as
                        # input-0.apk (base), input-1.apk, ... for one install session.
                        # Exactly that set must be there: a missing, extra or linked
                        # file would otherwise install a different package than asked.
                        [ -f count ] && [ ! -L count ] || { echo 'Split package without its APK count'; exit 2; }
                        read -r count < count
                        case "$count" in
                            ''|*[!0-9]*|0*|?????*) echo 'Invalid split APK count'; exit 2;;
                        esac
                        [ "$count" -ge 2 ] && [ "$count" -le 512 ] || { echo 'Invalid split APK count'; exit 2; }
                        index=0
                        while [ "$index" -lt "$count" ]; do
                            [ -f "input-$index.apk" ] && [ ! -L "input-$index.apk" ] || {
                                echo "Split APK $index is missing or is not a regular file; nothing was installed"; exit 2; }
                            index=$((index + 1))
                        done
                        found=0
                        for extra in input*.apk; do
                            [ -e "$extra" ] || [ -L "$extra" ] || continue
                            found=$((found + 1))
                        done
                        [ "$found" -eq "$count" ] || { echo 'Unexpected APK files in the request; nothing was installed'; exit 2; }
                        created=$(timeout 30 pm install-create -r --user 0)
                        result=$?
                        session=${created##*\[}
                        session=${session%%\]*}
                        case "$session" in
                            ''|*[!0-9]*) printf '%s\n' "$created"
                                echo 'Android did not open an install session; nothing was installed'; exit 1;;
                        esac
                        if [ "$result" -ne 0 ]; then
                            printf '%s\n' "$created"
                            timeout 15 pm install-abandon "$session" > /dev/null 2>&1
                            echo 'Android did not open an install session; nothing was installed'
                            exit 1
                        fi
                        index=0
                        while [ "$index" -lt "$count" ]; do
                            # Fixed text, no request data in it. The file is opened once;
                            # its type, size and content all come from that descriptor,
                            # and the whole step is bounded so a pipe cannot stall the queue.
                            if ! written=$(timeout 150 sh -c 'exec < "$3" || exit 1
                                    [ -f /proc/self/fd/0 ] || { echo "Not a regular file"; exit 1; }
                                    size=$(stat -L -c %s /proc/self/fd/0) || exit 1
                                    exec pm install-write -S "$size" "$1" "$2"' sh "$session" "$index.apk" "input-$index.apk"); then
                                printf '%s\n' "$written"
                                timeout 15 pm install-abandon "$session" > /dev/null 2>&1
                                echo 'Android rejected a split APK; nothing was installed'
                                exit 1
                            fi
                            index=$((index + 1))
                        done
                        # No abandon after this point: a commit may still complete.
                        timeout 900 pm install-commit "$session"
                        result=$?
                    else
                        [ -f input.apk ] && [ ! -L input.apk ] || { echo 'No APK in request'; exit 2; }
                        timeout 900 pm install -r --user 0 "$base/$work/input.apk"
                        result=$?
                    fi
                    if [ "$result" -eq 124 ] || [ "$result" -eq 137 ]; then
                        echo 'Android did not confirm the installation in time; it may still complete. Refresh before retrying.'
                    fi
                    [ "$result" -eq 0 ] || exit 1 ;;
                force-stop)
                    case "$package" in
                        android|com.android.*|org.lineageos.*|org.anvildroid.*|id.waydro.*)
                            echo 'Protected component'; exit 2;;
                    esac
                    am force-stop --user 0 "$package" ;;
                uninstall)
                    case "$package" in
                        android|com.android.*|org.lineageos.*|org.anvildroid.*|id.waydro.*|cu.axel.smartdock)
                            echo 'Protected component'; exit 2;;
                    esac
                    third_party=$(pm list packages -3 --user 0 "$package") || exit 1
                    printf '%s\n' "$third_party" | grep -Fxq "package:$package" || {
                        echo 'Cannot uninstall a system or protected app'; exit 2;
                    }
                    response=$(timeout 15 pm uninstall --user 0 "$package")
                    result=$?
                    printf '%s\n' "$response"
                    if [ "$result" -ne 0 ] || [ "$response" != Success ]; then
                        echo "Android uninstall did not complete (exit $result). Refresh before retrying."
                        exit 1
                    fi
                    remaining=$(pm list packages --user 0 "$package") || exit 1
                    if printf '%s\n' "$remaining" | grep -Fxq "package:$package"; then
                        echo 'Android still reports the app installed; refresh before retrying'
                        exit 1
                    fi ;;
                *) echo 'Unsupported operation'; exit 2;;
            esac
        )
        result=$?
        mv "$work/output.tmp" "$work/output"
        echo "$result" > "$work/status.tmp"
        mv "$work/status.tmp" "$work/status"
        # Host owns cleanup after consuming the atomic status marker.
    done
    wait_tick 0.1
done
