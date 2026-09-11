CHART      := charts/posthog
UPSTREAM   := $(shell awk '/^ref:/ {print $$2}' $(CHART)/upstream.lock 2>/dev/null)
NAMESPACE  ?= posthog
RELEASE    ?= posthog

.PHONY: help sync sync-check test-tools lint unittest kubeconform template docs e2e-install e2e-test e2e-clean

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

sync: ## Regenerate chart data from upstream (UPSTREAM=<commit sha>)
	cd tools && uv run python sync_upstream.py --ref $(UPSTREAM)

sync-check: ## Fail when upstream.yaml is not what rules.yaml + the locked upstream produce
	cd tools && uv run python sync_upstream.py --ref $(UPSTREAM) --no-digests --check

test-tools: ## Generator unit tests
	cd tools && uv run pytest -q

lint: ## helm lint
	helm lint $(CHART)
	helm lint $(CHART) -f $(CHART)/ci/kind-values.yaml

unittest: ## helm-unittest
	helm unittest $(CHART)

template: ## Render with default values
	helm template $(RELEASE) $(CHART)

kubeconform: ## Validate rendered manifests against the Kubernetes schemas
	helm template $(RELEASE) $(CHART) | kubeconform -strict -summary -schema-location default -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
	@for f in $(CHART)/ci/*.yaml; do echo "== $$f"; helm template $(RELEASE) $(CHART) -f $$f | kubeconform -strict -summary -schema-location default -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'; done

docs: ## Regenerate charts/posthog/README.md values table
	helm-docs --chart-search-root $(CHART) --template-files README.md.gotmpl

e2e-install: ## Install into the current kube context (kind in CI)
	kubectl create namespace $(NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -
	helm upgrade --install $(RELEASE) $(CHART) -n $(NAMESPACE) -f $(CHART)/ci/kind-values.yaml --wait --timeout 30m

e2e-test: ## Smoke tests against the installed release
	python3 e2e/smoke.py --namespace $(NAMESPACE) --release $(RELEASE)

e2e-clean: ## Remove the release
	helm uninstall $(RELEASE) -n $(NAMESPACE) || true
