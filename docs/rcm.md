# RCM-300 local relay support

This fork adds four local relay switches to SiHAS Canary v2.1.1. Other device
implementations are inherited from upstream. The `sihas` integration domain,
configuration entries, device identifier and initial patch's relay unique IDs
are intentionally unchanged.

## Verified device and protocol

The mapping was verified on an unloaded RCM-300 reporting firmware **03.07**
and **CFG=00**. A public manufacturer register map was not located. Verification
used write acknowledgements and fresh register reads; physical contact continuity
was not measured independently. Other firmware and the older RXM model have not
been verified.

- Transport: local UDP, port 502, using the existing SiHAS packet format.
- Read: function `0x03`, start register 0, count 64, 137-byte response.
- Write: function `0x06`, exact request echo followed by a fresh state read.
- Values: `0` = OFF, `1` = ON.

| Output | Zero-based register |
| --- | --- |
| Relay 1 | 0 |
| Relay 2 | 1 |
| Relay 3 | 2 |
| Relay 4 | 3 |

All four switches are created even when CFG is zero. Access is serialized so a
write and its readback cannot interleave with another relay operation. Reads
reject wrong lengths, transaction IDs, protocol/function fields and byte counts.
Failed communication marks a relay unavailable; successful polling recovers it.
A command is only reported as successful after its acknowledgement and readback
match. A timeout or error can still occur after the hardware applied a write;
check the actual device state before retrying a consequential operation.

## Installation and preserving existing entities

1. Enable local communication (ST/HA) in the SiHAS app and reserve the device's
   IP address in the router.
2. Back up Home Assistant settings before changing the installed source.
3. Add `https://github.com/brain-choi/sihas-canary` through Marketplace's **Add
   from link** option (or HACS custom repositories) and select a published release.
   Only one repository may install the `sihas` domain at a time. Switch ownership
   away from the original repository first; do not delete the SiHAS configuration
   entry in **Devices & services**. Inspect the uninstall dialog before proceeding
   and preserve existing configuration entries if offered.
4. Restart Home Assistant after replacing an already-loaded integration.
5. For a new installation only, add SiHAS with device type `RCM`, its IP and MAC,
   and the device's reported CFG. Existing configured devices need no re-adding.
6. Confirm all four entities are present, available and retain their original IDs.

Device identifier: `("sihas", device_mac)`.
Relay unique IDs: `RCM-{device_mac}-relay-1` through `RCM-{device_mac}-relay-4`.
Keep these stable when changing names, code or package sources.

## Tests

Run the offline suite without installing Home Assistant or contacting hardware:

```sh
python -m unittest discover -s tests -v
python -m compileall -q custom_components/sihas
```

Tests exercise the production RCM code, packet builder, register parser and UDP
sender against a simulated device. Only Home Assistant's entity base, exception
interface and shared constant definitions are substituted. They cover per-channel
ON/OFF, invalid inputs, response corruption, stale transactions, readback mismatch,
packet loss and retry exhaustion, availability recovery, stable identifiers and
serialized command/readback. They do not replace real Home Assistant lifecycle
or hardware validation. GitHub Actions runs them with Python 3.13 and 3.14.

## Releases and upstream updates

This fork publishes its own stable release series, starting at **v2.1.2**. Keep
`manifest.json` and `.bumpversion.toml` in sync with each release. Updates are
installed from published releases of this fork, not from upstream releases.

Fetch upstream changes, review/merge them without dropping RCM support, run the
offline suite, and validate the four switches in Home Assistant before publishing
an update. Preserve the upstream BSD-3-Clause license and attribution.
