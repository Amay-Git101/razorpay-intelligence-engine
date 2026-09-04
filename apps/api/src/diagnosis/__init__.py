"""AI diagnosis layer: classifies WHY a payment failed.

Deliberately its own top-level package rather than a submodule of
intelligence/. intelligence/ is the deterministic decision engine; this
package is the only place in the codebase that talks to a language model.
Keeping the boundary visible in the directory tree is the point -- an
architecture test asserts that no policy, action, or verification module
imports from here.
"""
