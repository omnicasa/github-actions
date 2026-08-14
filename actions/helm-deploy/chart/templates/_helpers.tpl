{{/*
Expand the name of the app.

Defaults to the RELEASE name, not the chart name. This is the single most
important line in the chart.

`chart.name` feeds app.kubernetes.io/name, which feeds chart.selectorLabels,
which feeds Deployment.spec.selector.matchLabels — an IMMUTABLE field. The
upstream scaffold defaults this to .Chart.Name, which was correct when every app
had its own chart named after itself. With one shared chart named omnicasa-app,
that default would rewrite every existing release's selector to
`app.kubernetes.io/name: omnicasa-app` and helm upgrade would fail outright with
"field is immutable".

Defaulting to .Release.Name reproduces the old per-app value exactly, because
every release is named after its app. Do not "simplify" this back.
*/}}
{{- define "chart.name" -}}
{{- default .Release.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name. With chart.name == .Release.Name the `contains` branch
is always taken, so this collapses to the release name — which is what every
existing release's Service, Ingress, ConfigMap and Secret are already called.
*/}}
{{- define "chart.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Release.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "chart.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels. Applied to every object's metadata, never to a selector.
*/}}
{{- define "chart.labels" -}}
helm.sh/chart: {{ include "chart.chart" . }}
{{ include "chart.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/*
Selector labels. IMMUTABLE once a Deployment exists — adding a key here breaks
every upgrade of every existing release. commonLabels deliberately does NOT
merge into this.
*/}}
{{- define "chart.selectorLabels" -}}
app.kubernetes.io/name: {{ include "chart.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "chart.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "chart.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
The image reference. `registry` is optional so `helm template` works locally
without one; the workflow always sets it.
*/}}
{{- define "chart.image" -}}
{{- if .Values.image.registry -}}
{{ .Values.image.registry }}/{{ .Values.image.repository }}:{{ .Values.image.tag }}
{{- else -}}
{{ .Values.image.repository }}:{{ .Values.image.tag }}
{{- end -}}
{{- end }}

{{/*
envFrom block shared by the Deployment, the migration Job and any CronJob, so
all three see byte-identical configuration. A migration Job reading a different
DATABASE_URL than the app is a failure mode worth designing out.
*/}}
{{- define "chart.envFrom" -}}
- configMapRef:
    name: {{ include "chart.fullname" . }}-config
- secretRef:
    name: {{ include "chart.fullname" . }}-secrets
{{- with .Values.extraEnvFrom }}
{{ toYaml . }}
{{- end }}
{{- end }}
