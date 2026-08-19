BENTO_IMAGE := ghcr.io/warpstreamlabs/bento:1.20.0

# On Windows/Git Bash, MSYS rewrites the container-side "/bento.yaml" path
# into a Windows path before it reaches docker. Harmless no-op on Linux/macOS.
export MSYS_NO_PATHCONV := 1

.PHONY: test-bento lint

test-bento:
	docker run --rm -v "$(CURDIR)/bento/fertloops.yaml:/bento.yaml" $(BENTO_IMAGE) test /bento.yaml

lint:
	docker run --rm -v "$(CURDIR)/bento/fertloops.yaml:/bento.yaml" $(BENTO_IMAGE) lint /bento.yaml
