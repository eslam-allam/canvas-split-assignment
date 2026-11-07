"""Canvas Split Assignment — split rubric-graded work into one assignment per CLO.

For every assignment you select, this tool:

  1. reads the assignment's rubric and extracts a CLO from each criterion
     using the regular expression you provide;
  2. creates one "No Submission" assignment per unique CLO, named
     ``<assignment name> - [<CLO>]`` (created unpublished);
  3. walks the graded submissions, splits each student's rubric points per CLO
     and posts the totals as that student's grade in the matching CLO assignment.

A submission is only split when its recorded grade equals the total of its
rubric assessment (within 0.01 points).  A grade reduced by exactly the
automatic late penalty is fine: the penalty is spread evenly across the CLOs.
Anything else (extra credit, manual override, ...) is reported and skipped, so
the CLO columns always reconcile with the gradebook.
"""

from .cli import main

__version__ = "0.1.0"

__all__ = ["main", "__version__"]
