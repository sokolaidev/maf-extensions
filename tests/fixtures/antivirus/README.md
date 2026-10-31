# Signed ClamAV test databases

These unmodified fixtures come from [ClamAV 1.4.6](https://github.com/Cisco-Talos/clamav/tree/5853e1396777bae4d909cd3dd44f58352e7cf9b3/unit_tests/input/freshclam_testfiles), commit `5853e1396777bae4d909cd3dd44f58352e7cf9b3`. The upstream input directory's GPL-2.0 license is retained in `COPYING`.

| File | SHA-256 |
| --- | --- |
| `test-1.cvd` | `a39c001ff5a02af09afc204f5710b58290dc68bdc76b6343011188179bf4d7cd` |
| `test-2.cdiff` | `dfae880d593d5a18c2a7c04a31c7fdd6a06e128866e7d1e10dd61fa6480b0927` |
| `test-3.cdiff` | `7428cddfb1e804df1cfef04d06736b6a1ce415a9ef9043746d9bc9a8237f2caa` |

`scripts/antivirus_cdiff_check.py` runs these fixtures through the actual guest preparation and scan code using the candidate image's native ClamAV. The signed archive embeds `test.info`; the fixture adapter maps metadata lookups and native patch filenames to that prefix, preserving signed bytes. Native authentication, patch application and scanning execute normally. Each of the main, daily and bytecode routes exercises a valid two-patch chain, a damaged signature with a matching transfer hash, and a metadata version mismatch injected after successful native authentication/application to isolate the guest's version guard.

The fixture clock places the signed daily database between eight and nine days old under a fourteen-day ceiling. The production guest must accept the native engine's exact seven-day advisory after enforcing authenticated freshness, and return clean. The bytecode route must reach directory loading with `--bytecode-unsigned=yes`; the fixture contains ordinary test signatures, so this is not evidence of current production bytecode execution or a current daily update chain.

Both Docker and ACAS qualification require these nine offline checks before upload cases. They need no host mirror. Production freshness and current host updates are checked separately by the upload cases.
