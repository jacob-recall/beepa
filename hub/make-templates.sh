#!/usr/bin/env bash
# hub/make-templates.sh — DEV TOOL, run once (or after a config change) by the
# maintainer. Freezes the current known-good hub config into git-tracked
# templates under hub/templates/, with every secret replaced by a ${PLACEHOLDER}.
# It also captures the CURRENT secret values into synapse/.hub-secrets.local so
# the very first render on this machine reproduces the existing stack exactly
# (and re-runs reuse them). Never prints a secret; verifies no secret leaks into
# a template before finishing.
#
#   Source (gitignored, real secrets)     ->  Template (tracked, placeholders)
#   synapse/homeserver.yaml                    hub/templates/synapse/homeserver.yaml.tmpl
#   synapse/<b>-registration.yaml              hub/templates/synapse/<b>-registration.yaml.tmpl
#   <b>/config.yaml                            hub/templates/<b>/config.yaml.tmpl
#
# The render side is hub/render-hub.sh (run at install time by setup.sh).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 "$(dirname "${BASH_SOURCE[0]}")/_make_templates.py" "${HERE}"
