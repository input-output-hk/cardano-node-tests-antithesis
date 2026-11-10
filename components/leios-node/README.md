# Pinned Leios epoch-information backport

The Leios node at `6a540bd43a83e697fd662b973f7e99cee02a59fb` pins consensus
`1820edf5e496fbec5aa230a8bee56397c76f474b`. Its certified-endorser-block replay
path takes an unfinished Dijkstra ledger configuration, leaving the throwing
`dummyEpochInfo` placeholder in use. Epoch-dependent transactions can then
terminate the node. Antithesis run `42792621201` observed this on all producers.

`epoch-config` applies only upstream consensus fix
[`e1aa48f000b30cafb7bc96f2fdb3ae91898eb32a`](https://github.com/IntersectMBO/ouroboros-consensus/commit/e1aa48f000b30cafb7bc96f2fdb3ae91898eb32a).
The original node flake and lock file remain pinned. The Docker layer replaces
the node executable and adds its runtime closure to the existing pinned image;
the environment, CLI, ledger types and wire encodings are unchanged. Image
labels record the upstream node revision, fix commit and patch checksum.

Build for local validation:

```sh
bash scripts/build-leios-epoch-fix.sh
```

This compiles with two jobs and four cores per job. Dependencies use the pinned
upstream caches; the patched node itself needs compilation. The default command
does not publish an image or change the workflow. Validate certified governance
transactions and node restart/replay locally before publishing.

When publishing is authorized, `--push` pushes the image, reads its registry
digest back and updates only the pinned Leios image in the workflow. It refuses
to replace an unexpected existing pin. Conway configuration and the separate
`leios-latest` image selection are untouched.

Local validation on 2026-11-10 compiled the patched node and ran its actual image
on fresh accelerated Leios state (`k=8`, 800 slots per epoch). Governance setup,
two isolated valid BLS pool registrations, load, 15 endorser-block certification
events and a producer restart completed through epoch 3. All producers continued
adding blocks after the restart.

A dedicated overflow scenario used fresh state with `k=4`, 400 slots per epoch,
8,000-byte ranking blocks and a 4,000-byte transaction limit. Four private,
zero-stake pools were registered, followed by 80 valid re-registrations of one
pool. Full signed-transaction references were matched to EB bodies, their
announcements and certifying blocks on the immutable chain: 20 transactions had
certification witnesses on the patched node. After retaining the producer's
ledger snapshots under another name, a crash restart replayed from genesis to
100% and the producer continued following the chain. These smaller limits are
local stress settings; the production genesis is unchanged.

Five offline tests cover build/publication guards and cleanup. The original
binary also completed this local scenario, so the exact Antithesis fault trigger
has not been reproduced. The source defect and upstream remedy are established,
and the patched path is exercised, but this does not guarantee a green paid run.
