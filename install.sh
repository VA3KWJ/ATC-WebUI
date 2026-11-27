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
        fail "Install Python 3.10+, curl, and CA certificates with your package manager, then rerun this installer."
    fi
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

    "$python" - "$work/source.tar.gz" "$python" <<'PY'
import os
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


def install(archive_path, python):
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
    print(f"Start Atomic WebUI with: {shlex.quote(str(data / 'run'))}")


try:
    install(*sys.argv[1:])
except (OSError, KeyError, ValueError, tarfile.TarError) as error:
    sys.exit(f"Installation failed: {error}")
PY
}

main "$@"
