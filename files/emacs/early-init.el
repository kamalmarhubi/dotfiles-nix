;; Elpaca requires this to be disabled before it loads. And
(setq package-enable-at-startup nil)
(defvar k/emacs-data-directory (expand-file-name "emacs/" "~/.local/share"))

(when (fboundp 'startup-redirect-eln-cache)
  (startup-redirect-eln-cache
   (convert-standard-filename
    (expand-file-name  "var/eln-cache/" k/emacs-data-directory))))
