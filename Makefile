CHART      := charts/posthog
UPSTREAM   := $(shell awk '/^ref:/ {print $$2}' $(CHART)/upstream.lock 2>/dev/null)
NAMESPACE  ?= posthog
RELEASE    ?= posthog

# PostHog's image as pinned in upstream.yaml (repository@digest), the tag an overlay built from this
# tree is published under (upstream commit, then a hash of overlay/Dockerfile and the module), and
# the name a local build gets.
POSTHOG_IMAGE = $(shell cd tools && uv run --locked python -c "import yaml; i = yaml.safe_load(open('../$(CHART)/upstream.yaml'))['images']['posthog']; print(i['repository'] + '@' + i['digest'])")
OVERLAY_HASH = $(shell python3 overlay/source_hash.py)
OVERLAY_TAG = $(UPSTREAM)-$(OVERLAY_HASH)
OVERLAY_IMAGE ?= posthog-k8s/posthog-web:dev

.PHONY: help sync sync-check test-tools test-overlay overlay-base overlay-tag overlay-build overlay-check lint unittest kubeconform template docs e2e-install e2e-test e2e-clean

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

sync: ## Regenerate chart data from upstream (UPSTREAM=<commit sha>)
	cd tools && uv run --locked python sync_upstream.py --ref $(UPSTREAM)

sync-check: ## Fail when upstream.yaml is not what rules.yaml + the locked upstream produce
	cd tools && uv run --locked python sync_upstream.py --ref $(UPSTREAM) --no-digests --check

test-tools: ## Generator unit tests
	cd tools && uv run --locked pytest -q

test-overlay: ## Unit tests of the trusted-header login module (overlay/)
	cd tools && uv run --locked pytest -q ../overlay/tests

overlay-base: ## Print the PostHog image the overlay is built on
	@echo $(POSTHOG_IMAGE)

overlay-tag: ## Print the tag an overlay built from this tree is published under
	@echo $(OVERLAY_TAG)

overlay-build: ## Build the web overlay image on the pinned PostHog image (OVERLAY_IMAGE=name:tag)
	docker build --build-arg POSTHOG_IMAGE=$(POSTHOG_IMAGE) --build-arg SOURCE_HASH=$(OVERLAY_HASH) -t $(OVERLAY_IMAGE) overlay

overlay-check: ## Fail when authProxy.image is set but is not the pinned overlay of the locked upstream commit and this tree's module
	@img=$$(awk '/^authProxy:/ {f=1} f && /^  image:/ {print $$2; exit}' $(CHART)/values.yaml | tr -d '"'); \
	if [ -z "$$img" ]; then echo "overlay-check: authProxy.image is empty, no overlay is published for this chart yet"; exit 0; fi; \
	case "$$img" in \
	  *":$(OVERLAY_TAG)@sha256:"*) echo "overlay-check ok" ;; \
	  *) echo "authProxy.image ($$img) is not repository:$(OVERLAY_TAG)@sha256:<digest>: the upstream commit or overlay/ changed since it was built. Rebuild and pin it, or empty authProxy.image"; exit 1 ;; \
	esac

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

caddy-validate: ## Render the router Caddyfile and validate it with the caddy binary (needs docker)
	helm template $(RELEASE) $(CHART) -s templates/router.yaml | python3 -c 'import sys,yaml; [print(d["data"]["Caddyfile"]) for d in yaml.safe_load_all(sys.stdin) if d and d["kind"]=="ConfigMap"]' > /tmp/Caddyfile
	docker run --rm -v /tmp/Caddyfile:/etc/caddy/Caddyfile:ro caddy:2.10-alpine caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

docs: ## Regenerate charts/posthog/README.md values table
	helm-docs --chart-search-root $(CHART) --template-files README.md.gotmpl

e2e-install: ## Install into the current kube context (kind in CI), printing pod status while waiting
	NAMESPACE=$(NAMESPACE) RELEASE=$(RELEASE) CHART=$(CHART) EXTRA_VALUES=$(EXTRA_VALUES) e2e/install.sh

e2e-export-seed: ## Export a schema seed (pg_dump + ClickHouse backup) from the installed release
	NAMESPACE=$(NAMESPACE) RELEASE=$(RELEASE) e2e/export-seed.sh .seed

e2e-test: ## Smoke tests against the installed release
	python3 e2e/smoke.py --namespace $(NAMESPACE) --release $(RELEASE)

e2e-clean: ## Remove the release
	helm uninstall $(RELEASE) -n $(NAMESPACE) || true
