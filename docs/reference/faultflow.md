# Faultflow

Retargeting a real `faultflow --export-patterns` export through an inserted network; see
[Faultflow pattern retargeting](../guide/faultflow-retargeting.md) for a full example.

::: warptap.faultflow_retarget
    options:
      members: [FaultflowRetargetError, retarget_faultflow_patterns]

## Continuous-flow decompressor

A faultflow design whose `[compression]` entry says `architecture = "continuous"` loads its
chains from a ring generator that takes `num_channels` fresh channel bits on every shift,
not one seed. `retarget_decompressed_faultflow_patterns` picks the retarget from the
manifest's architecture (an entry without one is the older one-shot decompressor);
`solve_pattern_channels` solves a pattern's load over every free bit (`num_channels` x
`warmup + max_chain_length`) strictly, and the retarget writes one channel word per shift
to the channel instrument, with one clock pulse each.

::: warptap.faultflow_continuous
    options:
      members: [ContinuousDecompressor, continuous_from_manifest, continuous_scan_in_rows, injector_stages, solve_pattern_channels, retarget_continuous_faultflow_patterns, retarget_decompressed_faultflow_patterns]
