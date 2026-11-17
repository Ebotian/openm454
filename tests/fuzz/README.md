# Atheris fuzz targets

These are Linux-only Atheris scripts, not pytest tests. They are excluded from
the normal suite and run in the nightly fuzz job:

    python tests/fuzz/fuzz_packet.py -max_total_time=300 -rss_limit_mb=2048
    python tests/fuzz/fuzz_adapter.py -max_total_time=300 -rss_limit_mb=2048

A crash is promoted to a seed under corpus/ and, where possible, to a fixed
Hypothesis example.
