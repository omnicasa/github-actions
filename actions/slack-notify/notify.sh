#!/usr/bin/env bash
set -euo pipefail

if [ -z "${WEBHOOK_URL:-}" ]; then
  echo "Slack webhook-url is blank, skipping notification"
  exit 0
fi

case "$STATUS" in
  success) color="#2eb886" ;;
  failure) color="#d93f0b" ;;
  *) color="#dbab09" ;;
esac

# "Key: Value" lines -> section fields. A line without a colon still gets a field
# (empty value) rather than aborting the whole notification. Slack rejects a fields
# array longer than 10 outright, so the tail is dropped rather than the message.
fields_json="[]"
if [ -n "${FACTS:-}" ]; then
  fields_json=$(printf '%s\n' "$FACTS" | jq -Rn '
    def mrkdwn: gsub("&"; "&amp;") | gsub("<"; "&lt;") | gsub(">"; "&gt;");
    [ inputs
      | select(. != "")
      | capture("^(?<title>[^:]+):\\s*(?<value>.*)$")? // {title: ., value: ""}
      | {type: "mrkdwn", text: "*\(.title|mrkdwn)*\n\(.value|mrkdwn)"}
    ][:10]
  ')
fi

body_json=$(jq -n \
  --arg title "$TITLE" \
  --arg color "$color" \
  --argjson fields "$fields_json" \
  --arg runUrl "${RUN_URL:-}" \
  '
  def mrkdwn: gsub("&"; "&amp;") | gsub("<"; "&lt;") | gsub(">"; "&gt;");
  ($title | mrkdwn) as $t |
  {
    # Block-only payloads preview as empty in the sidebar and in push notifications;
    # this top-level text is what Slack shows there.
    text: $title,
    attachments: [{
      color: $color,
      blocks: (
        # Slack link syntax is <url|text>, not markdown — the headline itself becomes
        # the link when a run-url is given, as well as the button below.
        [{type: "section", text: {type: "mrkdwn", text: (if $runUrl != "" then "*<\($runUrl)|\($t)>*" else "*\($t)*" end)}}]
        + (if ($fields | length) > 0 then [{type: "section", fields: $fields}] else [] end)
        + (if $runUrl != "" then [{type: "actions", elements: [{type: "button", text: {type: "plain_text", text: "Open run", emoji: true}, url: $runUrl}]}] else [] end)
      )
    }]
  }')

# Set only by CI, to assert the payload builds and is well-formed without a webhook.
if [ "${NOTIFY_DRY_RUN:-}" = "1" ]; then
  printf '%s\n' "$body_json"
  exit 0
fi

curl -sSf -X POST -H "Content-Type: application/json" -d "$body_json" "$WEBHOOK_URL" -o /dev/null
