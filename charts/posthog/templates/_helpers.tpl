{{/*
Chart data generated from upstream: charts/posthog/upstream.yaml
*/}}
{{- define "posthog.upstream" -}}
{{- .Files.Get "upstream.yaml" -}}
{{- end -}}

{{- define "posthog.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "posthog.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "posthog.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "posthog.labels" -}}
helm.sh/chart: {{ include "posthog.chart" . }}
app.kubernetes.io/name: {{ include "posthog.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | trunc 12 | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: posthog
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/* usage: include "posthog.selectorLabels" (list $ "web") */}}
{{- define "posthog.selectorLabels" -}}
{{- $ := index . 0 -}}{{- $component := index . 1 -}}
app.kubernetes.io/name: {{ include "posthog.name" $ }}
app.kubernetes.io/instance: {{ $.Release.Name }}
app.kubernetes.io/component: {{ $component }}
{{- end -}}

{{/* usage: include "posthog.serviceName" (list $ "web") */}}
{{- define "posthog.serviceName" -}}
{{- $ := index . 0 -}}{{- $component := index . 1 -}}
{{- printf "%s-%s" (include "posthog.fullname" $) $component | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Datastore host: the external host when configured, otherwise the bundled service.
usage: include "posthog.host" (list $ "postgresql")
*/}}
{{- define "posthog.host" -}}
{{- $ := index . 0 -}}{{- $key := index . 1 -}}
{{- $bundled := dict "postgresql" "postgresql" "redis" "redis7" "valkey" "valkey" "kafka" "kafka" "clickhouse" "clickhouse" "temporal" "temporal" -}}
{{- $cfg := index $.Values $key | default dict -}}
{{- $ext := $cfg.external | default dict -}}
{{- if $ext.host -}}
{{- $ext.host -}}
{{- else -}}
{{- include "posthog.serviceName" (list $ (index $bundled $key)) -}}
{{- end -}}
{{- end -}}

{{- define "posthog.siteHost" -}}
{{- regexReplaceAll "^https?://" .Values.posthog.siteUrl "" | regexReplaceAll "/.*$" "" -}}
{{- end -}}

{{- define "posthog.publicUrl" -}}
{{- .Values.posthog.publicUrl | default .Values.posthog.siteUrl | trimSuffix "/" -}}
{{- end -}}

{{/* S3 endpoint: external when configured, otherwise the bundled SeaweedFS service. */}}
{{- define "posthog.objectStorageEndpoint" -}}
{{- if .Values.objectStorage.endpoint -}}
{{- .Values.objectStorage.endpoint | trimSuffix "/" -}}
{{- else -}}
{{- printf "http://%s:8333" (include "posthog.serviceName" (list . "seaweedfs")) -}}
{{- end -}}
{{- end -}}

{{- define "posthog.publicHost" -}}
{{- regexReplaceAll "^https?://" (include "posthog.publicUrl" .) "" | regexReplaceAll "/.*$" "" -}}
{{- end -}}

{{/*
Image reference for an upstream image key. Digest pinning is on by default; values.images.overrides
can replace repository, tag or digest per key. usage: include "posthog.image" (list $ "web")
*/}}
{{- define "posthog.image" -}}
{{- $ := index . 0 -}}{{- $key := index . 1 -}}
{{- $up := include "posthog.upstream" $ | fromYaml -}}
{{- $img := index $up.images $key | default dict -}}
{{- $ov := index $.Values.images.overrides $key | default dict -}}
{{- $repo := $ov.repository | default $img.repository -}}
{{- $tag := $ov.tag | default $img.tag -}}
{{- $digest := $ov.digest | default $img.digest -}}
{{- if $.Values.global.imageRegistry -}}
{{- $repo = printf "%s/%s" (trimSuffix "/" $.Values.global.imageRegistry) (regexReplaceAll "^docker.io/" $repo "") -}}
{{- end -}}
{{- if and $digest $.Values.images.pinDigests (not $ov.tag) -}}
{{- printf "%s:%s@%s" $repo $tag $digest -}}
{{- else -}}
{{- printf "%s:%s" $repo $tag -}}
{{- end -}}
{{- end -}}

{{- define "posthog.imagePullSecrets" -}}
{{- with .Values.global.imagePullSecrets }}
imagePullSecrets:
{{- range . }}
  - name: {{ . }}
{{- end }}
{{- end }}
{{- end -}}

{{/*
Where a secret key lives. Order: explicit mapping in values.secrets.keys, well-known external
secrets (Postgres URL, object storage), then the chart-managed secret.
usage: include "posthog.secretRef" (list $ "DATABASE_URL") -> "name/key"
*/}}
{{- define "posthog.secretRef" -}}
{{- $ := index . 0 -}}{{- $key := index . 1 -}}
{{- $m := index $.Values.secrets.keys $key | default dict -}}
{{- $pg := $.Values.postgresql.external -}}
{{- $os := $.Values.objectStorage -}}
{{- $sr := $.Values.sessionReplay.storage -}}
{{- if $m.secret -}}
{{- printf "%s/%s" $m.secret ($m.key | default $key) -}}
{{- else if and (eq $key "DATABASE_URL") $pg.existingSecret $pg.urlKey -}}
{{- printf "%s/%s" $pg.existingSecret $pg.urlKey -}}
{{- else if and (eq $key "PGPASSWORD") $pg.existingSecret $pg.passwordKey -}}
{{- printf "%s/%s" $pg.existingSecret $pg.passwordKey -}}
{{- else if and (eq $key "OBJECT_STORAGE_ACCESS_KEY_ID") $os.existingSecret -}}
{{- printf "%s/%s" $os.existingSecret $os.accessKeyIdKey -}}
{{- else if and (eq $key "OBJECT_STORAGE_SECRET_ACCESS_KEY") $os.existingSecret -}}
{{- printf "%s/%s" $os.existingSecret $os.secretAccessKeyKey -}}
{{- else if and (eq $key "SESSION_RECORDING_ACCESS_KEY_ID") $sr.existingSecret -}}
{{- printf "%s/%s" $sr.existingSecret $sr.accessKeyIdKey -}}
{{- else if and (eq $key "SESSION_RECORDING_SECRET_ACCESS_KEY") $sr.existingSecret -}}
{{- printf "%s/%s" $sr.existingSecret $sr.secretAccessKeyKey -}}
{{- else if and (eq $key "SESSION_RECORDING_ACCESS_KEY_ID") (not $sr.accessKeyId) -}}
{{- include "posthog.secretRef" (list $ "OBJECT_STORAGE_ACCESS_KEY_ID") -}}
{{- else if and (eq $key "SESSION_RECORDING_SECRET_ACCESS_KEY") (not $sr.secretAccessKey) -}}
{{- include "posthog.secretRef" (list $ "OBJECT_STORAGE_SECRET_ACCESS_KEY") -}}
{{- else if and (has $key (list "SECRET_KEY" "ENCRYPTION_SALT_KEYS" "BROWSERLESS_TOKEN" "INTERNAL_API_SECRET")) $.Values.posthog.existingSecret -}}
{{- printf "%s/%s" $.Values.posthog.existingSecret $key -}}
{{- else -}}
{{- printf "%s/%s" (include "posthog.envSecretName" $) $key -}}
{{- end -}}
{{- end -}}

{{- define "posthog.envSecretName" -}}
{{- printf "%s-env" (include "posthog.fullname" .) -}}
{{- end -}}

{{/*
Render a list of env entries from upstream.yaml (value / tpl / secret) plus extras.
usage: include "posthog.env" (list $ $svcName $envList)
*/}}
{{- define "posthog.env" -}}
{{- $ := index . 0 -}}{{- $svc := index . 1 -}}{{- $env := index . 2 -}}
{{- range $env }}
- name: {{ .name }}
{{- if .secret }}
{{- $ref := splitn "/" 2 (include "posthog.secretRef" (list $ .secret)) }}
  valueFrom:
    secretKeyRef:
      name: {{ $ref._0 }}
      key: {{ $ref._1 }}
{{- if .optional }}
      optional: true
{{- end }}
{{- else if .tpl }}
  value: {{ tpl (.value | toString) $ | quote }}
{{- else }}
  value: {{ .value | toString | quote }}
{{- end }}
{{- end }}
{{- end -}}

{{/* ConfigMap name for an upstream config directory, e.g. docker/clickhouse -> <fullname>-cfg-clickhouse */}}
{{- define "posthog.configMapName" -}}
{{- $ := index . 0 -}}{{- $dir := index . 1 -}}
{{- $short := regexReplaceAll "^docker/" $dir "" | replace "/" "-" | replace "_" "-" -}}
{{- printf "%s-cfg-%s" (include "posthog.fullname" $) $short | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* The set of upstream config directories that become ConfigMaps. */}}
{{- define "posthog.configDirs" -}}
{{- $dirs := dict -}}
{{- range $path, $_ := .Files.Glob "files/upstream/docker/**" -}}
{{- $rel := trimPrefix "files/upstream/" $path -}}
{{- $_ := set $dirs (dir $rel) true -}}
{{- end -}}
{{- keys $dirs | sortAlpha | toJson -}}
{{- end -}}

{{/*
Pod-level security context and container security context for a service.
usage: include "posthog.podSecurityContext" (list $ $svc)
*/}}
{{- define "posthog.podSecurityContext" -}}
{{- $ := index . 0 -}}{{- $svc := index . 1 -}}
{{- $sc := $svc.securityContext | default dict -}}
{{- $ov := ($svc.overrides).podSecurityContext | default dict -}}
{{- $merged := mergeOverwrite (dict "runAsNonRoot" true "seccompProfile" (dict "type" "RuntimeDefault")) $sc $ov -}}
{{- if hasKey $merged "fsGroup" -}}{{- $_ := set $merged "fsGroupChangePolicy" "OnRootMismatch" -}}{{- end -}}
{{- toYaml $merged -}}
{{- end -}}

{{- define "posthog.containerSecurityContext" -}}
{{- $ := index . 0 -}}{{- $svc := index . 1 -}}
{{- $ov := ($svc.overrides).containerSecurityContext | default dict -}}
{{- $base := dict "allowPrivilegeEscalation" false "capabilities" (dict "drop" (list "ALL")) "readOnlyRootFilesystem" (default false ($svc.overrides).readOnlyRootFilesystem) -}}
{{- toYaml (mergeOverwrite $base $ov) -}}
{{- end -}}

{{/* Standard pod annotations: checksum of config so pods roll on config changes. */}}
{{- define "posthog.podAnnotations" -}}
{{- $ := index . 0 -}}{{- $svc := index . 1 -}}
checksum/upstream: {{ include "posthog.upstream" $ | sha256sum | trunc 16 }}
checksum/values: {{ toYaml $.Values.posthog | sha256sum | trunc 16 }}
{{- with $.Values.podAnnotations }}
{{ toYaml . }}
{{- end }}
{{- with ($svc.overrides).podAnnotations }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/* Init container that copies directories shipped inside the posthog image (IDL, UDF binaries). */}}
{{- define "posthog.imageDirInitContainer" -}}
{{- $ := index . 0 -}}{{- $mounts := index . 1 -}}
- name: copy-upstream
  image: {{ include "posthog.image" (list $ "posthog") | quote }}
  imagePullPolicy: IfNotPresent
  command: ["/bin/sh", "-ec"]
  args:
    - |
{{- range $mounts }}
{{- if .imageDir }}
      cp -r /code/{{ .imageDir }}/. /upstream/{{ .imageDir | replace "/" "-" }}/
{{- end }}
{{- end }}
      ls -la /upstream
  volumeMounts:
{{- range $mounts }}
{{- if .imageDir }}
    - name: upstream-{{ .imageDir | replace "/" "-" }}
      mountPath: /upstream/{{ .imageDir | replace "/" "-" }}
{{- end }}
{{- end }}
  securityContext:
    allowPrivilegeEscalation: false
    capabilities: {drop: [ALL]}
    runAsNonRoot: true
    runAsUser: 10001
    runAsGroup: 10001
  resources:
    requests: {cpu: 10m, memory: 64Mi}
    limits: {memory: 256Mi}
{{- end -}}
