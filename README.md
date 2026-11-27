# Atomic Server WebUI

A lightweight browser interface for an existing Atomic Server public API. It provides streaming text chat, model selection, browser-local conversation history, light and dark themes, and an optional image generator and gallery link. The WebUI uses Python 3.10+ and the standard library.

## One-shot install

Run this as the user who will run the WebUI. The installer is available at the URL once `install.sh` and the application are published on `main`.

```sh
bash -o pipefail -c 'curl -q -fsSL --proto "=https" --proto-redir "=https" https://raw.githubusercontent.com/VA3KWJ/ATC-WebUI/main/install.sh | sh'
```

The command requires Bash, curl, and working HTTPS trust to fetch the installer. Bash's `pipefail` reports download failures. On systems without Bash, download the script with the same curl options and run `sh install.sh` after a successful download. The installer reads `/etc/os-release` (or `/usr/lib/os-release`) and checks for Python 3.10+ with SSL, curl, and CA certificates. Where needed it installs prerequisites through APT, DNF, pacman, APK, or Zypper using root, `sudo`, or `doas`. Use a release with Python 3.10+ available.

The script supports Debian and Ubuntu, Fedora, RHEL and compatible distributions, Arch, Alpine, openSUSE, and derivatives identified through `ID_LIKE`. It installs these files for the current user.

| Path with default XDG directories | Purpose |
| --- | --- |
| `~/.local/share/atc-webui/` | Application files and the `run` launcher |
| `~/.config/atc-webui/config.sh` | Per-user settings |
| `~/.config/atc-webui/api-token` | Empty token file on first install, owned by the current user with mode `0600` |

The installer honors absolute `XDG_DATA_HOME` and `XDG_CONFIG_HOME` values and prints the resulting paths. Use directories controlled by the installer account or root. Rerunning it updates the application and launcher and preserves the contents of the settings and token files. It leaves startup to the operator after configuration.

To pin both the script and the application archive to a published commit, set `ref` to the full commit SHA.

```sh
ATC_WEBUI_REF='<full-commit-sha>' bash -o pipefail -c '
  curl -q -fsSL --proto "=https" --proto-redir "=https" \
    "https://raw.githubusercontent.com/VA3KWJ/ATC-WebUI/$ATC_WEBUI_REF/install.sh" | sh
'
```

## Configure and run

1. Run `atc api status` on the Atomic Server host to find its public API origin. Edit `~/.config/atc-webui/config.sh` and set `ATC_API_URL` to that origin, for example `http://127.0.0.1:1339`. Use HTTPS for a network upstream.
2. Put an Atomic **public API client token** in `~/.config/atc-webui/api-token`. Keep only the token on one line and keep the file owned by the account running the WebUI with mode `0600`. If `ATC_API_TOKEN` is already exported in the shell, unset it when using the token file.
3. Start the WebUI and open [http://127.0.0.1:8080](http://127.0.0.1:8080).

   ```sh
   ~/.local/share/atc-webui/run
   ```

The launcher runs in the foreground. Press Ctrl+C to stop it. To rotate the token, replace the configured token file with the new token, retain ownership and mode `0600`, and restart the WebUI. The empty installation placeholder must be populated before startup.

If the API is reachable and no models are listed, make a suitable model available through Atomic Server and select **Refresh models**. Use the **Images** view after setting `ATC_WEBUI_IMAGE_MODEL`. Conversations and theme choices are stored in the current browser. A generated image preview lasts for the current page session.

## Manual installation from a checkout

Install Python 3.10+ and CA certificates using your distribution's package manager. Run these commands from the repository root to set up the token file for the first time.

```sh
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/atc-webui"
(umask 077; mkdir -p "$config_dir"; set -C; : > "$config_dir/api-token")
```

Populate the file with your token, then start the application with the public API origin for your installation.

```sh
unset ATC_API_TOKEN
ATC_API_URL='http://127.0.0.1:1339' \
ATC_API_TOKEN_FILE="$config_dir/api-token" \
python3 server.py
```

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `ATC_API_URL` | Required | Atomic public API origin without a route or query. HTTPS is required for network upstreams. |
| `ATC_API_TOKEN_FILE` | Recommended | Path to the token file, owned and readable by the WebUI user with no group or other permissions. |
| `ATC_API_TOKEN` | Optional alternative | Supply the bearer token through the service environment. Set exactly one token source. |
| `ATC_WEBUI_HOST` | `127.0.0.1` | WebUI bind address. |
| `ATC_WEBUI_PORT` | `8080` | WebUI listen port. |
| `ATC_WEBUI_PUBLIC_ORIGIN` | Unset | Browser-facing origin such as `https://webui.example.org`, without a trailing slash. Set this for a reverse proxy or wildcard bind. |
| `ATC_WEBUI_IMAGE_MODEL` | Unset | Atomic image model identifier. An empty value disables image generation. |
| `ATC_WEBUI_GALLERY_URL` | Unset | Optional gallery link presented in the Images view. |
| `ATC_WEBUI_REF` | `main` | Installer-only Git branch, tag, or commit to download. |

## Network access

For remote access, keep the WebUI bound to loopback behind an HTTPS reverse proxy. Require separate access authentication at the proxy. The Atomic API token authenticates only the WebUI's upstream requests.

Set `ATC_WEBUI_PUBLIC_ORIGIN` to the external HTTPS origin and preserve the incoming host. Disable proxy buffering for streamed replies and allow time for the application's request deadline. The WebUI bounds upstream I/O waits to 15 seconds and uses a 180-second connection deadline. This example belongs inside an existing Nginx HTTPS server configuration.

```nginx
location / {
    auth_basic "Atomic WebUI";
    auth_basic_user_file /etc/nginx/atc-webui.htpasswd;

    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $http_host;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_read_timeout 190s;
}
```

## Validation

```sh
python3 -m unittest discover -s tests -v
```

See the [API compatibility notes](docs/api-compatibility.md) for mock and live test results.
