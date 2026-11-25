.PHONY: help install install-all install-agents install-skill install-bin
.PHONY: install-claude install-codex install-cursor install-gemini
.PHONY: install-tool install-atomic uninstall test clean

SKILL = room
SRC = room.py
BINDIR = $(HOME)/.local/bin

# How many superseded installs to keep per destination
BACKUPS ?= 3

# Each install target re-invokes make rather than depending on a shared
# install-atomic. A shared prerequisite is only built once per run, so
# install-all would install to the first destination and silently skip
# the rest.

help:
	@echo "Targets:"
	@echo "  make install              - Install the skill to ~/.agents/skills (default)"
	@echo "  make install-all          - Install everywhere: ~/.agents, each agent, and $(BINDIR)"
	@echo "  make install-agents       - Install to ~/.agents/skills, shared by every agent"
	@echo "  make install-claude       - Install to every ~/.claude* profile"
	@echo "  make install-codex        - Install to every ~/.codex* profile"
	@echo "  make install-cursor       - Install to every ~/.cursor* profile"
	@echo "  make install-gemini       - Install to every ~/.gemini* profile"
	@echo "  make install-skill        - Install to agent/skills in this repo"
	@echo "  make install-bin          - Symlink room into $(BINDIR), so agents can type 'room'"
	@echo "  make uninstall            - Remove every installed copy"
	@echo "  make test                 - Run the test suite"
	@echo "  make clean                - Remove build artifacts"

install: install-agents ## Install to ~/.agents/skills

install-all: ## Install to every location
	@$(MAKE) --no-print-directory install-agents
	@$(MAKE) --no-print-directory install-claude
	@$(MAKE) --no-print-directory install-codex
	@$(MAKE) --no-print-directory install-cursor
	@$(MAKE) --no-print-directory install-gemini
	@$(MAKE) --no-print-directory install-bin

install-agents: ## Install to ~/.agents/skills, shared by every agent
	@$(MAKE) --no-print-directory install-atomic DST=$(HOME)/.agents/skills

install-claude: TOOL = claude
install-codex: TOOL = codex
install-cursor: TOOL = cursor
install-gemini: TOOL = gemini

install-claude install-codex install-cursor install-gemini: install-tool

# Each tool can have several profile directories side by side, such as
# ~/.claude, ~/.claude-rh and ~/.claude-mst. Install to every one of them.
# test -d skips both an unmatched glob and files like ~/.claude.json.
install-tool: ## Install to every ~/.$(TOOL)* directory
	@test -n "$(TOOL)"
	@found=; \
	for dir in $(HOME)/.$(TOOL)*; do \
		test -d "$$dir" || continue; \
		found=1; \
		$(MAKE) --no-print-directory install-atomic DST="$$dir/skills" || exit 1; \
	done; \
	test -n "$$found" || echo "No $(HOME)/.$(TOOL)* directory, skipped"

install-skill: ## Install to agent/skills in this repo
	@$(MAKE) --no-print-directory install-atomic DST=agent/skills
	@echo "(accessible via .claude/skills and .codex/skills symlinks)"

install-atomic:
	@test -n "$(DST)"
	@mkdir -p $(DST)-backup
	@mkdir -p $(DST)/$(SKILL)
	@TMPDIR=$$(mktemp -d $(DST)-backup/XXXXXX) && \
		mkdir -p "$$TMPDIR/$(SKILL)/scripts" && \
		cp skills/$(SKILL)/*.md "$$TMPDIR/$(SKILL)/" && \
		cp $(SRC) "$$TMPDIR/$(SKILL)/scripts/$(SKILL)" && \
		chmod +x "$$TMPDIR/$(SKILL)/scripts/$(SKILL)" && \
		mv --exchange "$$TMPDIR/$(SKILL)" $(DST) && \
		echo "Old version backed up at $$TMPDIR/$(SKILL)"
	@cd $(DST)-backup && \
		stale=$$(ls -1dt */ 2>/dev/null | tail -n +$$(($(BACKUPS) + 1))) && \
		if test -n "$$stale"; then \
			echo "$$stale" | xargs rm -rf && \
			echo "Pruned $$(echo "$$stale" | wc -l) older backups"; \
		fi
	@echo "Installed to $(DST)/$(SKILL)"

install-bin: ## Symlink room into $(BINDIR)
	@mkdir -p $(BINDIR)
	@ln -sf $(CURDIR)/$(SRC) $(BINDIR)/$(SKILL)
	@echo "Linked $(BINDIR)/$(SKILL) -> $(CURDIR)/$(SRC)"
	@case ":$$PATH:" in *":$(BINDIR):"*) ;; \
		*) echo "Note: $(BINDIR) is not on your PATH" ;; esac

uninstall: ## Remove every installed copy
	@for dst in $(HOME)/.agents*/skills $(HOME)/.claude*/skills \
	            $(HOME)/.codex*/skills $(HOME)/.cursor*/skills \
	            $(HOME)/.gemini*/skills agent/skills; do \
		if test -d "$$dst/$(SKILL)"; then \
			rm -rf "$$dst/$(SKILL)" && echo "Removed $$dst/$(SKILL)"; \
		fi; \
	done
	@test ! -L $(BINDIR)/$(SKILL) || \
		(rm -f $(BINDIR)/$(SKILL) && echo "Removed $(BINDIR)/$(SKILL)")

test: ## Run the test suite
	@python3 -m unittest discover -s tests -v

clean: ## Remove build artifacts
	@rm -rf __pycache__ tests/__pycache__
	@rm -rf agent/skills/$(SKILL)
