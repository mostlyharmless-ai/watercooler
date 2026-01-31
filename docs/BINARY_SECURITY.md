# Binary Download Security Model

This document describes the security model for downloading and verifying external binaries
used by watercooler-cloud, specifically the llama.cpp `llama-server` binary used for local
LLM inference.

## Overview

When local embedding or LLM services are enabled, watercooler-cloud can automatically
download pre-built binaries from trusted sources. This document explains the security
measures in place to protect against supply chain attacks.

## Threat Model

We protect against:
- **Tampered binaries**: Attacker modifies a release binary to include malware
- **MITM attacks**: Attacker intercepts download and substitutes malicious binary
- **Path traversal**: Malicious archive containing files that escape extraction directory
- **Compromised checksums**: Attacker modifies both binary and checksum file

We do NOT protect against:
- Compromised upstream repository (ggml-org/llama.cpp)
- Zero-day vulnerabilities in the binary itself
- Attacks requiring local system access

## Security Measures

### 1. SHA256 Checksum Verification

All downloaded binaries are verified against known-good SHA256 checksums before execution.

**Checksum Registry** (`startup.py:LLAMA_SERVER_CHECKSUMS`):
```python
LLAMA_SERVER_CHECKSUMS = {
    "b7896": {
        "ubuntu-x64": "329a716c5fb216d49d674d3ac7a9aab90d04942d80b08786aeaaae49a4490b93",
        "macos-arm64": "231f8f7ff3763de2ab1cbeb097e728e4bb442b0bc941f6dacc7ef83d01ae47bb",
        # ...
    },
}
```

**Verification Modes** (`WATERCOOLER_LLAMA_SERVER_VERIFY`):
- `strict`: Fail if checksum is unknown or mismatched (most secure)
- `warn` (default): Warn if checksum unknown, fail if mismatched
- `skip`: Skip verification entirely (not recommended)

### 2. Path Traversal Prevention

Archive extraction validates that all member paths resolve within the destination directory:

```python
def _is_safe_archive_path(member_name: str, dest_dir: Path) -> bool:
    """Reject absolute paths and parent directory traversal."""
    if Path(member_name).is_absolute():
        return False
    full_path = (dest_dir / member_name).resolve()
    return full_path.is_relative_to(dest_dir.resolve())
```

This prevents attacks like:
- `../../../etc/cron.d/malware` - parent traversal
- `/usr/local/bin/malware` - absolute path injection

### 3. HTTPS-Only Downloads

All downloads use HTTPS to prevent man-in-the-middle attacks:
- GitHub releases: `https://github.com/ggml-org/llama.cpp/releases/download/...`
- Model files: `https://huggingface.co/...`

### 4. Trusted Sources Only

Binaries are only downloaded from:
- **ggml-org/llama.cpp**: Official llama.cpp releases on GitHub
- **Hugging Face**: Model weights only (not executables)

## Configuration Options

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `WATERCOOLER_LLAMA_SERVER_VERIFY` | `warn` | Verification mode: `strict`, `warn`, `skip` |
| `WATERCOOLER_LLAMA_SERVER_SHA256` | - | User-provided checksum (overrides registry) |
| `WATERCOOLER_LLAMA_SERVER_RELEASE` | (latest known) | Pin to specific release tag |
| `WATERCOOLER_LLAMA_SERVER_PATH` | - | Use local binary instead of downloading |

### Recommended Security Settings

For production environments:
```bash
# Strict verification - fail if checksum unknown
export WATERCOOLER_LLAMA_SERVER_VERIFY=strict

# Pin to known-good release
export WATERCOOLER_LLAMA_SERVER_RELEASE=b7896
```

For air-gapped environments:
```bash
# Provide pre-downloaded binary
export WATERCOOLER_LLAMA_SERVER_PATH=/opt/llama-server/llama-server
```

## Adding New Release Checksums

When a new llama.cpp release is needed, maintainers should:

1. Download official release assets:
   ```bash
   gh release download <tag> --repo ggml-org/llama.cpp \
     --pattern "llama-*-bin-*.tar.gz" --dir /tmp/verify
   ```

2. Compute checksums:
   ```bash
   sha256sum /tmp/verify/*.tar.gz
   ```

3. Verify the release (check GitHub for any security advisories)

4. Add to `LLAMA_SERVER_CHECKSUMS` in `startup.py`:
   ```python
   "b7900": {
       "ubuntu-x64": "<sha256>",
       "macos-arm64": "<sha256>",
       # ...
   },
   ```

## Incident Response

If a compromised binary is discovered:

1. Remove the affected release from `LLAMA_SERVER_CHECKSUMS`
2. Add the malicious checksum to a blocklist (if implemented)
3. Issue a security advisory
4. Users with `WATERCOOLER_LLAMA_SERVER_VERIFY=strict` are protected
5. Users with `warn` mode will see warnings if they have cached the bad binary

## Audit Trail

All download and verification events are logged:
- Download URL and response status
- Computed SHA256 of downloaded file
- Verification result (pass/fail/skip)
- Path traversal rejections (security warnings)

Enable debug logging to see full details:
```bash
export WATERCOOLER_LOG_LEVEL=DEBUG
```

## Related Documentation

- [Installation Guide](INSTALLATION.md) - General setup instructions
- [Configuration](CONFIGURATION.md) - All configuration options
- [Environment Variables](ENVIRONMENT_VARS.md) - Complete env var reference
