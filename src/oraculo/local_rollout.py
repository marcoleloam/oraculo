"""Read-only probe of the managed Oraculo installation and actual process."""

import argparse
import json
from pathlib import Path

from oraculo.protocol import content_hash
from oraculo.runtime import RuntimeError, current_release, digest, verify
from oraculo.service_control import ServiceController


def probe(root, config_path, environment, *, controller=None):
    selected = current_release(root)
    if selected is None:
        raise RuntimeError("release_missing")
    manifest = verify(root / "releases" / selected, installed=True)
    controller = controller or ServiceController(root, config_path)
    loaded = controller.inspect_launchd()
    observed = controller.request("runtime.probe", {})
    artifact = content_hash(manifest["files"])
    expected = dict(
        version=1,
        source_commit=manifest.get("delivery_source_commit", manifest["source_commit"]),
        merge_sha=manifest["source_commit"],
        release_id=selected,
        artifact_sha256=artifact,
        environment=environment,
    )
    healthy = (
        loaded.get("loaded") is True
        and type(loaded.get("pid")) is int
        and observed.get("pid") == loaded["pid"]
        and controller.loaded_matches(loaded, selected)
        and observed.get("release_id") == selected
        and observed.get("source_commit") == manifest["source_commit"]
        and observed.get("delivery_source_commit") == expected["source_commit"]
        and observed.get("artifact_sha256") == artifact
        and observed.get("config_path") == str(config_path.resolve())
        and observed.get("config_sha256") == digest(config_path)
        and observed.get("protocol_version") == 1
        and observed.get("liveness") is True
        and observed.get("readiness") is True
    )
    # Preserve the dispatch alert verbatim. It is a separate diagnostic, never
    # cleared, relabelled, or used as a substitute for process identity.
    return expected | dict(
        healthy=healthy,
        liveness=observed.get("liveness"),
        readiness=observed.get("readiness"),
        diagnostic_health=observed.get("diagnostic_health"),
        process_pid=observed.get("pid"),
        config_sha256=digest(config_path),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--environment", required=True)
    args = parser.parse_args(argv)
    try:
        result = probe(args.root, args.config, args.environment)
    except (OSError, ValueError, RuntimeError):
        result = dict(healthy=False, reason="local_destination_unverified")
    print(json.dumps(result))
    return 0 if result.get("healthy") else 1


if __name__ == "__main__":
    raise SystemExit(main())
