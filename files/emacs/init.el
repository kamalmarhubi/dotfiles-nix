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
 (elpaca-use-package-mode))

(use-package elpaca
  :no-require t  ; elpaca is required in the installer code above.
  :custom
  (elpaca-lock-file (expand-file-name "elpaca.lock" user-emacs-directory) "Set lock file location")
  :config
  (defun k/elpaca-write-lock-file ()
    (interactive)
    (elpaca-write-lock-file elpaca-lock-file))
  :hook (elpaca-post-queue . k/elpaca-write-lock-file))

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
