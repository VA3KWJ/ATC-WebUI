#!/bin/sh
# Install Atomic WebUI for the current Linux user.
set -eu

fail() {
    printf '%s\n' "$*" >&2
    exit 1
}

select_python() {
    for candidate in python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
        if command -v "$candidate" >/dev/null 2>&1 &&
            "$candidate" -c 'import ssl, sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
            python=$(command -v "$candidate")
            return 0
        fi
    done
    return 1
}

as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    elif command -v doas >/dev/null 2>&1; then
        doas "$@"
    else
        fail "This installer needs root, sudo, or doas to run: $1"
    fi
}

system_command() {
    PATH=/usr/sbin:/usr/bin:/sbin:/bin command -v "$1"
}

select_service_manager() {
    service_manager=
    pid_one=$(cat /proc/1/comm 2>/dev/null) || pid_one=
    if [ "$pid_one" = systemd ] && [ -d /run/systemd/system ]; then
        systemctl_path=$(system_command systemctl) || fail "systemctl is required to install the service."
        service_manager=systemd
    elif [ -f /run/openrc/softlevel ]; then
        system_command rc-service >/dev/null && system_command supervise-daemon >/dev/null ||
            fail "rc-service and supervise-daemon are required to install the OpenRC service."
        service_manager=openrc
    elif [ "$pid_one" = init ] && { [ -p /run/initctl ] || [ -p /dev/initctl ]; }; then
        system_command start-stop-daemon >/dev/null && system_command update-rc.d >/dev/null &&
            system_command flock >/dev/null ||
            fail "Debian-style start-stop-daemon, update-rc.d, and flock are required to install the SysVinit service."
        service_manager=sysv
    fi
}

register_service() {
    case "$service_manager" in
        systemd) destination=/etc/systemd/system/atc-webui.service; mode=0644 ;;
        openrc|sysv) destination=/etc/init.d/atc-webui; mode=0755 ;;
        *)
            printf '%s\n' "No supported init system is active. Use the foreground launcher."
            return
            ;;
    esac

    as_root /bin/sh -eu -c '
        PATH=/usr/sbin:/usr/bin:/sbin:/bin
        staged=$(mktemp "${2}.XXXXXX")
        trap '\''rm -f -- "$staged"'\'' EXIT
        trap '\''exit 1'\'' HUP INT TERM
        cat -- "$1" > "$staged"
        chmod "$3" "$staged"
        mv -fT -- "$staged" "$2"
    ' sh "$work/service" "$destination" "$mode" ||
        fail "Could not install the service definition at $destination."

    if [ "$service_manager" = systemd ]; then
        as_root "$systemctl_path" daemon-reload || fail "Installed $destination, but systemd could not reload it."
    fi
    prefix=
    if [ "$(id -u)" -ne 0 ]; then
        if command -v sudo >/dev/null 2>&1; then prefix='sudo '; else prefix='doas '; fi
    fi
    printf 'Installed service definition: %s\n' "$destination"
    printf '%s\n' "After configuring Atomic WebUI, enable it at boot and start it:"
    case "$service_manager" in
        systemd)
            printf '  %ssystemctl enable --now atc-webui\n  %ssystemctl status atc-webui\n  %ssystemctl restart atc-webui\n' "$prefix" "$prefix" "$prefix"
            ;;
        openrc)
            printf '  %src-update add atc-webui default\n  %src-service atc-webui start\n  %src-service atc-webui status\n  %src-service atc-webui restart\n' "$prefix" "$prefix" "$prefix" "$prefix"
            ;;
        sysv)
            printf '  %supdate-rc.d atc-webui defaults\n  %supdate-rc.d atc-webui enable\n  %sservice atc-webui start\n  %sservice atc-webui status\n  %sservice atc-webui restart\n' "$prefix" "$prefix" "$prefix" "$prefix" "$prefix"
            ;;
    esac
}

install_prerequisites() {
    case "$family" in
        debian)
            as_root apt-get update
            as_root apt-get install -y python3 curl ca-certificates
            ;;
        fedora)
            as_root dnf install -y python3 curl ca-certificates
            ;;
        rhel)
            if select_python; then
                as_root dnf install -y curl ca-certificates
            else
                as_root dnf install -y python3.11 curl ca-certificates
            fi
            ;;
        arch)
            as_root pacman -S --needed --noconfirm python curl ca-certificates
            ;;
        alpine)
            as_root apk add python3 curl ca-certificates
            ;;
        suse)
            as_root zypper --non-interactive install python3 curl ca-certificates
            ;;
    esac
}

main() {
    [ "$(uname -s)" = Linux ] || fail "This installer requires Linux."
    if [ -r /etc/os-release ]; then
        . /etc/os-release
    elif [ -r /usr/lib/os-release ]; then
        . /usr/lib/os-release
    else
        fail "Cannot determine the Linux distribution."
    fi

    family=
    for distro in "${ID:-}" ${ID_LIKE:-}; do
        case "$distro" in
            debian|ubuntu|linuxmint|pop|raspbian) family=debian ;;
            fedora) family=fedora ;;
            rhel|centos|rocky|almalinux|ol) family=rhel ;;
            arch|manjaro|endeavouros) family=arch ;;
            alpine) family=alpine ;;
            opensuse*|sles|suse) family=suse ;;
        esac
        [ -z "$family" ] || break
    done
    [ -n "$family" ] || fail "Unsupported distribution: ${PRETTY_NAME:-${ID:-unknown}}. See the README for manual installation."
    select_service_manager

    ref=${ATC_WEBUI_REF:-main}
    case "$ref" in
        .*|*[!A-Za-z0-9._-]*) fail "ATC_WEBUI_REF must be a simple branch, tag, or commit." ;;
    esac

    printf 'Detected %s.\n' "${PRETTY_NAME:-$ID}"
    if ! select_python || ! command -v curl >/dev/null 2>&1 ||
        ! "$python" -c 'import ssl, sys; sys.exit(not ssl.create_default_context().get_ca_certs())' 2>/dev/null; then
        install_prerequisites
    fi
    select_python || fail "Python 3.10 or newer with SSL support is required. Install it and rerun this installer."
    command -v curl >/dev/null 2>&1 || fail "curl is required to download Atomic WebUI."

    umask 077
    work=$(mktemp -d)
    trap 'rm -rf -- "$work"' EXIT
    trap 'exit 1' HUP INT TERM
    curl -q --proto '=https' --proto-redir '=https' --tlsv1.2 --fail --silent --show-error \
        --location --connect-timeout 10 --max-time 120 --max-filesize 10485760 \
        "https://codeload.github.com/VA3KWJ/ATC-WebUI/tar.gz/$ref" -o "$work/source.tar.gz"

    "$python" - "$work/source.tar.gz" "$python" "$service_manager" "$work/service" <<'PY'
import os
import pwd
import shlex
import stat
import sys
import tarfile
import tempfile
from pathlib import Path

FILES = ("server.py", "model_control.py", "static/index.html", "static/app.js", "static/style.css")


def check_ancestors(path):
    for ancestor in reversed(path.parents):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid not in {0, os.geteuid()}
            or (info.st_mode & 0o022 and not sticky_root)
        ):
            raise ValueError(f"An untrusted account can control {ancestor}. Choose private XDG directories.")


def install_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise ValueError(f"Expected a directory owned by the current user at {path}")
    path.chmod(0o700)


def private_file(path, contents):
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as existing:
            info = os.fstat(existing.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise ValueError(f"Expected a regular file owned by the current user at {path}")
            os.fchmod(existing.fileno(), 0o600)
    else:
        with os.fdopen(descriptor, "wb") as created:
            created.write(contents)


def replace_file(path, contents, mode):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        staged = Path(temporary.name)
        try:
            temporary.write(contents)
            temporary.close()
            staged.chmod(mode)
            staged.replace(path)
        finally:
            staged.unlink(missing_ok=True)


def xdg_path(variable, default):
    value = os.environ.get(variable)
    path = Path(value) if value else Path.home() / default
    if not path.is_absolute():
        raise ValueError(f"{variable} must be an absolute path")
    return path.resolve() / "atc-webui"


def systemd_quote(value):
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{value}"'


def service_definition(manager, data):
    account = pwd.getpwuid(os.geteuid())
    home = str(Path.home())
    launcher = str(data / "run")
    environment = {"HOME": home}
    for variable in ("XDG_DATA_HOME", "XDG_CONFIG_HOME"):
        if os.environ.get(variable):
            environment[variable] = str(xdg_path(variable, "").parent)
    paths = (*environment.values(), launcher)
    if any(ord(char) < 32 or ord(char) == 127 for path in paths for char in path):
        raise ValueError("Service paths cannot contain control characters")

    if manager == "systemd":
        env_lines = "".join(
            f"Environment={systemd_quote(f'{name}={value}')}\n"
            for name, value in environment.items()
        )
        exec_path = systemd_quote(launcher.replace("$", "$$"))
        return (
            "[Unit]\nDescription=Atomic WebUI\nAfter=network.target\n\n"
            f"[Service]\nUser={account.pw_uid}\nUMask=0077\n"
            f"{env_lines}ExecStart=/bin/sh {exec_path}\nRestart=on-failure\nRestartSec=5s\n\n"
            "[Install]\nWantedBy=multi-user.target\n"
        )

    if manager == "openrc":
        # OpenRC evaluates command_args as shell words when starting the daemon.
        args = shlex.join([*(f"{name}={value}" for name, value in environment.items()), "/bin/sh", launcher])
        return (
            "#!/sbin/openrc-run\n"
            "description='Atomic WebUI'\nsupervisor=supervise-daemon\n"
            f"command=/usr/bin/env\ncommand_user={shlex.quote(shlex.quote(account.pw_name))}\n"
            f"command_args={shlex.quote(args)}\numask=0077\n"
            "respawn_delay=5\nrespawn_max=5\nrespawn_period=60\n"
            "depend() { use net; }\n"
        )

    exports = "".join(f"export {name}={shlex.quote(value)}\n" for name, value in environment.items())
    return f"""#!/bin/sh
### BEGIN INIT INFO
# Provides:          atc-webui
# Required-Start:    $local_fs $remote_fs $network
# Required-Stop:     $local_fs $remote_fs $network
# Default-Start:     2 3 4 5
# Default-Stop:      0 1 6
# Short-Description: Atomic WebUI
### END INIT INFO
PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 077
{exports}launcher={shlex.quote(launcher)}
server={shlex.quote(str(data / 'server.py'))}
uid={account.pw_uid}
pidfile=/run/atc-webui.pid
identity=/run/atc-webui.identity

process_start_time() {{
    read -r proc_stat < "/proc/$1/stat" || return 1
    # The comm field can contain spaces and parentheses. The start time is
    # field 22, or field 20 after removing the PID and comm.
    set -f
    set -- ${{proc_stat##*) }}
    set +f
    case "${{1:-}}" in ''|Z|X) return 1 ;; esac
    [ "$#" -ge 20 ] || return 1
    shift 19
    printf '%s\\n' "$1"
}}

read_pid() {{
    read -r pid < "$pidfile" || return 1
    case "$pid" in ''|0|*[!0-9]*) return 1 ;; esac
}}

running_pid() {{
    [ -f "$pidfile" ] && [ -f "$identity" ] && read_pid || return 1
    {{
        read -r started_pid && read -r started_uid && read -r started_time
    }} < "$identity" || return 1
    [ "$pid" = "$started_pid" ] &&
        [ "$(process_start_time "$pid" 2>/dev/null)" = "$started_time" ] &&
        start-stop-daemon --status --pid "$pid" --user "$started_uid"
}}

start() {{
    if running_pid; then return 0; fi
    rm -f -- "$pidfile" "$identity" || return
    start-stop-daemon --start --background --make-pidfile --pidfile "$pidfile" \\
        --chuid "$uid" --startas /bin/sh -- "$launcher" 9>&- || return
    for attempt in 1 2 3 4 5 6 7 8 9 10; do
        sleep 0.1
        if [ -f "$pidfile" ] && read_pid &&
            started_time=$(process_start_time "$pid" 2>/dev/null) &&
            start-stop-daemon --status --pid "$pid" --user "$uid" &&
            grep -Fzxq -- "$server" "/proc/$pid/cmdline" 2>/dev/null; then
            printf '%s\\n' "$pid" "$uid" "$started_time" > "$identity"
            return
        fi
    done
    printf '%s\\n' 'Atomic WebUI did not start. Check the configuration with the foreground launcher.' >&2
    return 1
}}

stop() {{
    if running_pid; then
        start-stop-daemon --stop --oknodo --retry TERM/10/KILL/5 \\
            --pid "$pid" --user "$started_uid" || return
    fi
    rm -f -- "$pidfile" "$identity"
}}

case "${{1:-}}" in
    start|stop|restart|force-reload|status) ;;
    *) printf 'Usage: %s {{start|stop|restart|force-reload|status}}\\n' "$0" >&2; exit 2 ;;
esac
exec 9>/run/atc-webui.lock || exit 1
flock -x 9 || exit 1
case "$1" in
    start) start ;;
    stop) stop ;;
    restart|force-reload) stop && start ;;
    status)
        if running_pid; then
            printf 'Atomic WebUI is running (PID %s).\\n' "$pid"
        else
            printf '%s\\n' 'Atomic WebUI is stopped.'
            exit 3
        fi
        ;;
esac
"""


def install(archive_path, python, manager, service_path):
    python = os.path.abspath(python)
    if Path(archive_path).stat().st_size > 10 * 1024 * 1024:
        raise ValueError("Source archive exceeds 10 MiB")
    with tarfile.open(archive_path, "r:gz") as archive:
        root = archive.next()
        if root is None or not root.isdir() or "/" in root.name:
            raise ValueError("Unexpected source archive layout")
        source = {}
        for name in FILES:
            member = archive.getmember(f"{root.name}/{name}")
            if not member.isfile() or not 0 < member.size <= 1024 * 1024:
                raise ValueError(f"Unexpected source archive entry for {name}")
            with archive.extractfile(member) as item:
                source[name] = item.read()

    data = xdg_path("XDG_DATA_HOME", ".local/share")
    config = xdg_path("XDG_CONFIG_HOME", ".config")
    if data == config or data in config.parents or config in data.parents:
        raise ValueError("The installation and configuration directories must be separate")
    if manager:
        Path(service_path).write_text(service_definition(manager, data), encoding="utf-8")
    check_ancestors(data)
    check_ancestors(config)
    for directory in (data, data / "static", config):
        install_directory(directory)

    token = config / "api-token"
    settings = config / "config.sh"
    private_file(token, b"")
    private_file(settings, (
        "# Set this to the Atomic public API origin shown by atc api status.\n"
        "# Example: http://127.0.0.1:1339\n"
        "ATC_API_URL=''\n"
        f"ATC_API_TOKEN_FILE={shlex.quote(str(token))}\n"
        "# ATC_WEBUI_PORT=8080\n"
    ).encode())

    for name, contents in source.items():
        replace_file(data / name, contents, 0o600)
    launcher = (
        "#!/bin/sh\nset -eu\nset -a\n"
        f". {shlex.quote(str(settings))}\n"
        f"set +a\nexec {shlex.quote(python)} {shlex.quote(str(data / 'server.py'))}\n"
    )
    replace_file(data / "run", launcher.encode(), 0o700)
    print(f"Configuration: {settings}")
    print(f"API token file: {token} (mode 0600)")
    print("Set ATC_API_URL and put your Atomic public API client token in the token file.")
    print(f"Foreground launcher: {shlex.quote(str(data / 'run'))}")


try:
    install(*sys.argv[1:])
except (OSError, KeyError, ValueError, tarfile.TarError) as error:
    sys.exit(f"Installation failed: {error}")
PY
    register_service
}

main "$@"
