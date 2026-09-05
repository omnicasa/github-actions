#!/usr/bin/env bash
set -euo pipefail

if [ -z "${WEBHOOK_URL:-}" ]; then
  echo "MS Teams webhook-url is blank, skipping notification"
  exit 0
fi

case "$STATUS" in
  success) color="Good" ;;
  failure) color="Attention" ;;
  *) color="Warning" ;;
esac

# "Key: Value" lines -> FactSet entries. A line without a colon still gets a
# fact (empty value) rather than aborting the whole notification.
facts_json="[]"
if [ -n "${FACTS:-}" ]; then
  facts_json=$(printf '%s\n' "$FACTS" | jq -Rn '
    [inputs | select(. != "") | capture("^(?<title>[^:]+):\\s*(?<value>.*)$")? // {title: ., value: ""}]
  ')
fi

body_json=$(jq -n \
  --arg title "$TITLE" \
  --arg color "$color" \
  --argjson facts "$facts_json" \
  --arg runUrl "${RUN_URL:-}" \
  '{
    type: "message",
    attachments: [{
      contentType: "application/vnd.microsoft.card.adaptive",
      content: {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        type: "AdaptiveCard",
        version: "1.4",
        body: (
          [{type: "TextBlock", text: $title, weight: "Bolder", size: "Medium", color: $color, wrap: true}]
          + (if ($facts | length) > 0 then [{type: "FactSet", facts: $facts}] else [] end)
        ),
        actions: (if $runUrl != "" then [{type: "Action.OpenUrl", title: "Open run", url: $runUrl}] else [] end)
      }
    }]
  }')

curl -sSf -X POST -H "Content-Type: application/json" -d "$body_json" "$WEBHOOK_URL" -o /dev/null
