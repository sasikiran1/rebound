# Freeze record: ollama-recovery-v1

Recorded before the first block trial. Configuration under test: the tree at commit 3af7c23bc0ff845c7f6ab486a14540dd5ee4a9f3
(clean). Each file below must hash the same at the start of every block trial (scripts/write_freeze.py --check).

| File | SHA-256 | Same as frozen commit |
|---|---|---|
| docs/ollama_protocol.md | c9b0640f184da3882e780a582f43192b4a468fedd077566b576cf4a932755bc9 | yes |
| manifests/ollama/blocks_v1.specs | d9d1943e626be383fe47095640a08ebdeae6e447e487a2c3a8221aab05e36f35 | yes |
| manifests/ollama/recovery_gemma4_B.yaml | 31e23184bbe8f9f2a021a38d26f760c2ef22d712b51b0cc5e49144374c76c06d | yes |
| manifests/ollama/rate_rule.json | 0dd87ed0018dbb8b8dd81176625ebb7914ec298d66908e0e466b8919961c850a | yes |
| prompts/agent_v1.md | f643a6747f51e59cd3223910f254491abd1446f3ea787699ff9e9867b7b080c3 | yes |
| prompts/tools_v1.json | 7bd25cf4721f951c1d47dfd58790e43c6f3706dd28042613a317b45e2040e741 | yes |
| harness/ollama.py | a6e70ad07f3fba487fc1ba00dc694a8be75e55bce47d7e8e13fb9934557a8565 | yes |
| harness/agent.py | 097f42b685c7e45b8502916789de3569e5a8fc776eb9c9d17b59c16e53d714df | yes |
| harness/trial.py | b594633eb519a1f1e0d4f865c4d12cb7dee95b782e5f7add1352f09a6d79c663 | yes |
| harness/adapter.py | 69cceecf7da35fd27aedf3ba2442c88a56754cbba081c45c48484b74c9aa1eff | yes |
| backend/fielddesk/app.py | 916469613bdfc598b5544d15d84e904cfa58f1e98636bd1c0e0a25d2946dde57 | yes |
| backend/fielddesk/gate.py | 529f9033d468240d83f65e88f523834cc829aacda41550055dce2eee0823f977 | yes |
| analysis/metrics.py | a7d6c7a87f6ad9b65acaf7925c3324babae9056cadaa4f0865cf733287655424 | yes |
| analysis/recovery.py | 6153cc158619c176fc4212a35709af28b8aef64d0de00e7a58087553587dff84 | yes |
| analysis/ollama_campaign.py | 684edc920dce1edd39ee59f622542a789f26cc24e38c251dd909db5505f1f529 | yes |

Pilot (exploratory, task set A, not part of the comparison): runs/ollama-pilot-v1 tr_0001 (B) and tr_0002 (D), both valid.
Model: gemma4-12b-16k:latest, digest 61d4626bfb6cb772f62a94d562088ea34a427d32f86b20f15c13441823f19b54.
Block order: manifests/ollama/blocks_v1.specs (seeded shuffles 20261029-20261031).
