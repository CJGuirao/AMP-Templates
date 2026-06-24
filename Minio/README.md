# MinIO AMP Template

This is a CubeCoders AMP Generic module template for running MinIO as an AMP-managed machine.

## Files

- `minio.kvp` - AMP Generic module root template.
- `minioconfig.json` - AMP UI settings manifest.
- `minioports.json` - S3 API and web console ports.
- `minioupdates.json` - MinIO binary download/update stages.

## Production Data Path

The default MinIO data path is `/mnt/backups`, matching the requested production storage location. It is exposed as the `Data Path` setting in AMP and can be changed before startup.

Before starting the instance on Linux, make sure the path exists and is writable by the AMP instance user. For example, adapt ownership and permissions to your deployment policy:

```bash
sudo mkdir -p /mnt/backups
sudo chown -R amp:amp /mnt/backups
```

If running this AMP instance in Docker or Podman, mount `/mnt/backups` into the container at the same path or change the `Data Path` setting to the mounted container path.

## Install

For a local-only template, copy these files into an AMP deployment template repository folder such as:

```text
/__VDS__ADS01/Plugins/ADSModule/DeploymentTemplates/LOCAL-main/
```

Then refresh AMP and create a new instance using the MinIO template.

## Notes

- Change `Root User` and `Root Password` before exposing MinIO.
- The template uses AMP `FetchURL` updates because MinIO publishes direct release binaries rather than GitHub release assets.
- The S3 API defaults to TCP `9000`; the web console defaults to TCP `9001`.
- `Public S3 API URL` and `Public Console URL` are optional, but useful when MinIO sits behind a reverse proxy.
