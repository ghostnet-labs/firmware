# Kernel CI transient retry policy

`Retry transient kernel failures` watches completed `Build Kernel` workflow runs and may rerun failed jobs when every failure is attributable to a narrowly allowlisted upstream infrastructure/download error.

The retry workflow does not mark checks successful, skip required checks, or suppress failures. It only asks GitHub Actions to rerun the failed jobs for the same workflow run. A later attempt must pass normally for the protected gate to turn green.

Automatic retry is capped at three total attempts. If any failed job lacks an allowlisted transient signature, the workflow leaves the run failed for investigation.

Allowlisted classes include OpenWrt CDN/source mirror failures, truncated transfers, DNS/connection resets, temporary signature/mirror rotation failures, and download checksum mismatches associated with the upstream download path.
