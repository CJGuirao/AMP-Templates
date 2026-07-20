# AMP workshop templates

Maintained by **Antonia Guirao, lead developer**.

## Hermes Agent (Linux Docker instance)

The `cjguirao-hermes-agent` Generic template starts two supervised processes in one AMP instance:

- `hermes gateway run`, which keeps persistent sessions, messaging adapters, and the cron scheduler alive;
- `hermes dashboard`, exposed through AMP's **Hermes Web Dashboard** TCP port (default `9119`).

Hermes state is stored under `/AMP/hermes-agent/.hermes`, below the AMP application directory and therefore visible through AMP's file browser. It includes configuration, API keys, OAuth tokens, sessions, cron jobs, skills, memory, logs, and the package virtual environment.

### Deployment

1. In AMP, fetch this repository under **Configuration → Instance Deployment**.
2. Create the `cjguirao-hermes-agent` instance on a Linux AMP host with Docker-instance support enabled.
3. Set a unique dashboard username, a strong password, and a stable session secret. Generate the session secret with `openssl rand -base64 32`.
4. Complete the initial **Update** action. It creates `/AMP/hermes-agent/.hermes/venv`, checks PyPI, and installs `hermes-agent[all]`; the output reports the installed version.
5. Start the instance. Its startup bootstrap writes the supplied dashboard credentials into `/AMP/hermes-agent/.hermes/config.yaml` as the required `dashboard.basic_auth` provider, then launches the gateway and dashboard.
6. Open the allocated **Hermes Web Dashboard** port. Log in, then use the dashboard's **API Keys**, **Models**, **Config**, **Channels**, and **Cron** pages to complete configuration.

The dashboard is intentionally exposed only with the required username/password authentication. Put AMP itself and its assigned service ports behind a firewall, VPN, or reverse proxy appropriate to the deployment. Do not expose the optional OpenAI-compatible API server without its own random API key.

### Updates

Use AMP's **Update** button whenever you want to upgrade Hermes Agent. The template calls `/usr/local/bin/hermes-amp-update`, which:

1. reports the currently installed Hermes version;
2. checks PyPI;
3. installs the newest release when `Hermes Agent Version` is `latest`, or the selected exact PyPI version when pinned;
4. prints the resulting version.

Restart the AMP instance after a successful update. The persistent `/AMP/hermes-agent/.hermes` data directory is not replaced by an application update.

### Cron

The running gateway owns the scheduler. After a model/provider is configured, create and manage recurring jobs through the dashboard **Cron** page, the embedded browser chat, or `hermes cron` in the instance console. Scheduled jobs persist at `/AMP/hermes-agent/.hermes/cron/`.

For unattended cron jobs, explicitly select a provider/model in the job or re-pin jobs after changing the global model, so they do not silently switch providers.

### Image publishing

`.github/workflows/publish-hermes-amp-image.yml` publishes the supporting AMP-compatible image to `ghcr.io/cjguirao/amp-hermes-agent:latest`. It is deliberately derived from `cubecoders/ampbase:debian`, because AMP Generic custom images must retain the `/ampstart.sh` entrypoint. The image contains only bootstrap dependencies and supervisor; Hermes itself is installed into persistent instance storage by the AMP Update action.
