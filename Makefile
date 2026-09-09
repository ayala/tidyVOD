VERSION := 0.8.9
PYTHON ?= python3
RELEASE_DIR := releases/v$(VERSION)
PACKAGE := $(RELEASE_DIR)/tidyvod-v$(VERSION).zip
CHECKSUM := $(RELEASE_DIR)/SHA256SUMS
PLUGIN_FILES := __init__.py core.py plugin.py plugin.json logo.png

.PHONY: test test-orm package verify clean

test:
	$(PYTHON) -m unittest discover -s tests -v

test-orm:
	$(PYTHON) tests/orm_reconciliation.py

package: test test-orm
	@test ! -e $(RELEASE_DIR) || (echo "ERROR: $(RELEASE_DIR) already exists; releases are immutable." && exit 1)
	mkdir -p $(RELEASE_DIR)
	zip -q -r $(PACKAGE) $(PLUGIN_FILES) README.md LICENSE
	cd $(RELEASE_DIR) && shasum -a 256 $$(basename $(PACKAGE)) > $$(basename $(CHECKSUM))
	@echo "Built $(PACKAGE)"
	@echo "This release is immutable. Bump VERSION before the next package build."

verify:
	cd $(RELEASE_DIR) && shasum -a 256 -c $$(basename $(CHECKSUM))

clean:
	@echo "Nothing removed. Release artifacts are immutable and clean never deletes them."
