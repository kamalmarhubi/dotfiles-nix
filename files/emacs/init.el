;;; -*- lexical-binding: t; -*-

;; My changes from the version at https://github.com/progfolio/elpaca/blame/master/doc/installer.el
;; - elpaca-directory is ~/.local/share/emacs/elpaca
;; - remove depth stuff and clone with `--filter=tree:0` instead
;;
;; k/emacs-data-directory is defined in early-init.el
(defvar elpaca-installer-version 0.11)
(defvar elpaca-directory (expand-file-name "elpaca/" k/emacs-data-directory))
(defvar elpaca-builds-directory (expand-file-name "builds/" elpaca-directory))
(defvar elpaca-repos-directory (expand-file-name "repos/" elpaca-directory))
(defvar elpaca-order '(elpaca :repo "https://github.com/progfolio/elpaca.git"
                              :ref nil :inherit ignore
                              :files (:defaults "elpaca-test.el" (:exclude "extensions"))
                              :build (:not elpaca--activate-package)))
(let* ((repo  (expand-file-name "elpaca/" elpaca-repos-directory))
       (build (expand-file-name "elpaca/" elpaca-builds-directory))
       (order (cdr elpaca-order))
       (default-directory repo))
  (add-to-list 'load-path (if (file-exists-p build) build repo))
  (unless (file-exists-p repo)
    (make-directory repo t)
    (when (<= emacs-major-version 28) (require 'subr-x))
    (condition-case-unless-debug err
        (if-let* ((buffer (pop-to-buffer-same-window "*elpaca-bootstrap*"))
                  ((zerop (apply #'call-process `("git" nil ,buffer t "clone"
                                                  "--filter=tree:0"
                                                  ,(plist-get order :repo) ,repo))))
                  ((zerop (call-process "git" nil buffer t "checkout"
                                        (or (plist-get order :ref) "--"))))
                  (emacs (concat invocation-directory invocation-name))
                  ((zerop (call-process emacs nil buffer nil "-Q" "-L" "." "--batch"
                                        "--eval" "(byte-recompile-directory \".\" 0 'force)")))
                  ((require 'elpaca))
                  ((elpaca-generate-autoloads "elpaca" repo)))
            (progn (message "%s" (buffer-string)) (kill-buffer buffer))
          (error "%s" (with-current-buffer buffer (buffer-string))))
      ((error) (warn "%s" err) (delete-directory repo 'recursive))))
  (unless (require 'elpaca-autoloads nil t)
    (require 'elpaca)
    (elpaca-generate-autoloads "elpaca" repo)
    (let ((load-source-file-function nil)) (load "./elpaca-autoloads"))))
(add-hook 'after-init-hook #'elpaca-process-queues)
(elpaca `(,@elpaca-order))

;; Install & enable elpaca's use-package support.
(elpaca elpaca-use-package
 (elpaca-use-package-mode 1))

(use-package elpaca
  :no-require t  ; elpaca is required in the installer code above.
  :custom
  (elpaca-lock-file (expand-file-name "elpaca.lock" user-emacs-directory) "Set lock file location")
  :config
  (defun k/elpaca-write-lock-file ()
    (interactive)
    (elpaca-write-lock-file elpaca-lock-file))
  (defun k/elpaca--mark-init-if-from-init-file (order &optional queue)
    "Mark ORDER as init package when eval'd from a buffer visiting
user-init-file. This allows packages added to init.el to be included in
the lock file even when eval'd interactively rather than during Emacs
startup."
    (when-let* ((e (elpaca-get (elpaca--first order)))
                (file (buffer-file-name))
                ((file-equal-p file user-init-file)))
      (setf (elpaca<-init e) t)))
  (advice-add 'elpaca--queue :after #'k/elpaca--mark-init-if-from-init-file)
  (defun k/elpaca-unpin-and-update (id)
    "Remove pin from package ID's recipe and update it."
    (interactive (list (elpaca--read-queued "Unpin and update package: ")))
    (let ((e (or (elpaca-get id) (user-error "Package %S is not queued" id))))
      ;; Remove :ref, :pin, and :tag from the recipe
      (setf (elpaca<-recipe e)
            (cl-loop for (key val) on (elpaca<-recipe e) by #'cddr
                     unless (memq key '(:ref :pin :tag))
                     append (list key val)))
      ;; Now update the package
      (elpaca-update id t)))
  :hook (elpaca-after-init . (lambda ()
                               (k/elpaca-write-lock-file)
                               (add-hook 'elpaca-post-queue-hook #'k/elpaca-write-lock-file))))

(use-package emacs
  :custom
  (blink-cursor-mode nil)
  (inhibit-startup-screen t)
  (inhibit-startup-echo-area-message (user-login-name))
  (tab-bar-new-tab-choice "*scratch*")
  :config
  (recentf-mode 1)
  (savehist-mode 1)
  (repeat-mode 1)
  ;; Suppress elpaca core stale version warning
  (add-to-list 'warning-suppress-types '(elpaca core stale))
  (load-theme 'modus-operandi-tinted t))

(use-package no-littering
  ;; :wait is required to make sure this gets required before anything else
  ;; can make a mess of things.
  :ensure (:wait t)
  :demand t
  :no-require t  ; It's required in the preface just below.
  :preface
  (setq no-littering-etc-directory (expand-file-name "etc" user-emacs-directory)
        no-littering-var-directory (expand-file-name "var" k/emacs-data-directory))
  (require 'no-littering))

;; Get env vars from shell when in graphical mode.
(use-package exec-path-from-shell
  :ensure t
  :if (display-graphic-p)
  :custom (exec-path-from-shell-variables '("PATH" "MANPATH"))
  :config (exec-path-from-shell-initialize))

(defun k/op-read (ref)
  (string-trim (shell-command-to-string (format "op --account=PNGVKX37O5CSHJXPCJHSSAL4OQ read '%s'" ref))))

(defun k/make-op-reader (ref)
  (let ((cache nil))
    (lambda ()
      (or cache
          (setq cache (k/op-read ref))))))

(use-package transient
  :ensure t)

(use-package with-editor
  :ensure t
  :config
  (shell-command-with-editor-mode 1)
  (add-hook 'shell-mode-hook  'with-editor-export-editor)
  (add-hook 'eshell-mode-hook 'with-editor-export-editor)
  (add-hook 'term-exec-hook   'with-editor-export-editor)
  (add-hook 'vterm-mode-hook  'with-editor-export-editor))

;; Need 9.7+ for gptel's org-mode-branching-context to work.
(use-package org
  :ensure t
  :custom
  (org-todo-keywords '((sequence "TODO(t)" "PROG(p)" "|" "DONE(d)" "CNCL(c)")))
  (org-agenda-files '("~/wave/plan/wave.org")))

(use-package org-modern
  :ensure t
  :hook ((org-mode . org-modern-mode)
         (org-agenda-finalize . org-modern-agenda)))

(use-package org-contrib
  :ensure t
  :after org
  :custom
  (org-expiry-inactive-timestamps t)
  :config
  (require 'org-expiry)
  (org-expiry-insinuate))

(use-package org-hide-drawers
  :ensure t
  :after org
  :hook (org-mode . org-hide-drawers-mode))

(use-package org-reverse-datetree
  :ensure t
  :after org
  :init
  (setq-default org-reverse-datetree-level-formats '("%G-W%V" "[%Y-%m-%d %a]")))

(use-package org-capture
  :ensure nil
  :after org
  :bind ("C-c c" . org-capture)
  :custom
  (org-capture-templates
   ;; TODO: make this system context dependent somehow. Personal systems won't have wave.org.
   '(("t" "todo" entry
      (file+function "~/wave/plan/wave.org"
                     (lambda () (org-reverse-datetree-goto-date-in-file)))
      "* TODO %?"
      :empty-lines 1
      :hook org-expiry-insert-created)
     ("l" "log" entry
      (file+function "~/wave/plan/wave.org"
                     (lambda () (org-reverse-datetree-goto-date-in-file)))
      "* DONE %?"
      :empty-lines 1
      :hook org-expiry-insert-created)
     ("n" "note" entry
      (file+function "~/wave/plan/wave.org"
                     (lambda () (org-reverse-datetree-goto-date-in-file)))
      "* %?"
      :empty-lines 1
      :hook org-expiry-insert-created)
     ("s" "start of day" entry
      (file+function "~/wave/plan/wave.org"
                     (lambda () (org-reverse-datetree-goto-date-in-file)))
      "* TODO Start of day checklist
- [ ] What's my main focus today?
  %?
- [ ] Anything to prep or flag for standup?
- [ ] Any code reviews in my queue?
- [ ] Any follow-ups or responses I owe?"
      :empty-lines 1
      :hook org-expiry-insert-created)
     ("e" "end of day" entry
      (file+function "~/wave/plan/wave.org"
                     (lambda () (org-reverse-datetree-goto-date-in-file)))
      "* TODO End of day checklist
- [ ] Tickets updated with current state + next step?
- [ ] Did I do work that isn't captured? → log it
- [ ] Anything blocked or at risk? → flag before logging off
- [ ] Code reviews: caught up or falling behind?
- [ ] Did I work on my main focus, or get pulled elsewhere? → if elsewhere, is that visible?"
      :empty-lines 1
      :hook org-expiry-insert-created)
     ("S" "start of week" entry
      (file+function "~/wave/plan/wave.org"
                     (lambda () (org-reverse-datetree-goto-date-in-file)))
      "* TODO Start of week checklist
- [ ] What's committed this week?
- [ ] Is it broken down into tickets before I start?
- [ ] WIP check: how many things am I juggling? (aim for 2-3)
- [ ] Any schedule or availability changes to communicate?"
      :empty-lines 1
      :hook org-expiry-insert-created)
     ("E" "end of week" entry
      (file+function "~/wave/plan/wave.org"
                     (lambda () (org-reverse-datetree-goto-date-in-file)))
      "* TODO End of week checklist
- [ ] What did I actually ship/complete?
- [ ] Anything I said I'd do that didn't happen? → flagged + why?
- [ ] How did code reviews go this week?
- [ ] Visibility gut-check: would my manager know what I did without asking?"
      :empty-lines 1
      :hook org-expiry-insert-created))))

(use-package gptel
  :ensure t

  :commands (gptel gptel-send gptel-menu)
  :custom
  (gptel-default-mode 'org-mode)
  (gptel-model 'claude-3-7-sonnet-20250219)
  (gptel-prompt-prefix-alist
          '((markdown-mode . "REQ> ")
            (org-mode . "REQ> ")
            (text-mode . "REQ> ")))
  (gptel-response-prefix-alist
          '((markdown-mode . "RES> ")
            (org-mode . "RES> ")
            (text-mode . "RES> ")))

  (gptel-org-branching-context t)
  :config
  (setopt gptel-backend (gptel-make-anthropic "Claude" :stream t :key (k/make-op-reader "op://Private/Anthropic/credential"))))

(use-package agent-shell
  :ensure t
  :custom
  (agent-shell-preferred-agent-config 'claude-code)
  (agent-shell-show-welcome-message nil)
  (agent-shell-display-action '((display-buffer-in-side-window)
                                (side . right)
                                (slot . 0)
                                (window-width . 0.4)
                                (dedicated . t)
                                (window-parameters . ((no-delete-other-windows . t)))))
  :config
  (defun k/agent-shell-bookmark-make-record ()
    (unless (derived-mode-p 'agent-shell-mode)
      (error "Not in an agent shell buffer"))
    (let* ((agent-config (map-elt (agent-shell--state) :agent-config))
           (identifier (map-elt agent-config :identifier)))
      `(,(buffer-name)
        (handler . k/agent-shell-bookmark-jump)
        (agent-identifier . ,identifier)
        (working-directory . ,(agent-shell-cwd)))))

  (defun k/agent-shell-bookmark-jump (bookmark)
    (let* ((identifier (bookmark-prop-get bookmark 'agent-identifier))
           (mode-line-name (bookmark-prop-get bookmark 'agent-mode-line-name))
           (working-directory (bookmark-prop-get bookmark 'working-directory))
           ;; Find config by identifier or mode-line-name (for backwards compatibility), falling
           ;; back to preferred config.
           (agent-config (or (when identifier
                               (seq-find (lambda (c) (eq (map-elt c :identifier) identifier))
					 agent-shell-agent-configs))
                             (when mode-line-name
                               (seq-find (lambda (c) (equal (map-elt c :mode-line-name) mode-line-name))
					 agent-shell-agent-configs))
			     agent-shell-preferred-agent-config)))
      (let ((default-directory working-directory))
        (agent-shell-start :config agent-config))))

  (add-hook 'agent-shell-mode-hook
            (lambda ()
              (setq-local bookmark-make-record-function #'k/agent-shell-bookmark-make-record))))

(use-package activities
  :ensure t
  :config
  (activities-mode 1)
  (activities-tabs-mode 1))

(use-package vertico
  :ensure t
  :custom
  (vertico-cycle t)
  (vertico-resize nil)
  :config
  (vertico-mode 1))

(use-package marginalia
  :ensure t
  :config
  (marginalia-mode 1))

(use-package orderless
  :ensure t
  :custom
  (completion-styles '(orderless basic)))

;; Consider adding bindings:
;;   https://github.com/minad/consult/blob/main/README.org#use-package-example
(use-package consult
  :ensure t)

(use-package embark
  :ensure t
  :bind
  (("C-." . embark-act)         ;; pick some comfortable binding
  ("C-;" . embark-dwim)        ;; good alternative: M-.
  ("C->" . embark-act-all)
  ("C-h B" . embark-bindings)) ;; alternative for `describe-bindings'
  :init
  (setq prefix-help-command #'embark-prefix-help-command))

(use-package embark-consult
  :ensure t ; only need to install it, embark loads it after consult if found
  :hook
  (embark-collect-mode . consult-preview-at-point-mode))

(use-package corfu
  :ensure t
  :custom
  (corfu-auto nil)
  :config
  (global-corfu-mode 1))

(use-package magit
  :ensure t
  :after transient
  :bind ("C-x g" . magit-status)
  :custom
  (magit-define-global-key-bindings 'recommended)
  (magit-refresh-status-buffer nil))

(use-package git-modes
  :ensure t)

(use-package forge
  :ensure t
  :after magit)

(use-package terraform-mode
  :ensure t)
