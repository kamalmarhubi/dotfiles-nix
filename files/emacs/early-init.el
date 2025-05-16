;;; -*- lexical-binding: t; -*-

;; Elpaca requires this to be disabled before it loads. And it doesn't seem to
;; get set early enough if set with setopt or customize-set-variable.
(setq package-enable-at-startup nil)

(defvar k/emacs-data-directory (expand-file-name "emacs/" "~/.local/share"))

;; From https://github.com/emacscollective/no-littering?tab=readme-ov-file#native-compilation-cache
(when (and (fboundp 'startup-redirect-eln-cache)
           (fboundp 'native-comp-available-p)
           (native-comp-available-p))
  (startup-redirect-eln-cache
   (convert-standard-filename
    (expand-file-name "var/eln-cache/" k/emacs-data-directory))))

;; Disable UI elements early to avoid momentary display
(push '(vertical-scroll-bars . nil) default-frame-alist)
(push '(horizontal-scroll-bars . nil) default-frame-alist)
(setq scroll-bar-mode nil
      horizontal-scroll-bar-mode nil)

(push '(tool-bar-lines . 0) default-frame-alist)
(push '(menu-bar-lines . 0) default-frame-alist)
(setq tool-bar-mode nil
      menu-bar-mode nil)
