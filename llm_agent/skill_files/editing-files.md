---
name: editing-files
description: How to edit files reliably: reading, patching, what to do when text is not found, and when to rewrite the whole file.
---
Read the file with read_file (line_numbers=true) before patching it, and copy 'old' exactly, as a few whole lines so it is unique.
If a patch says the text was not found, do NOT send it again. Use the numbered lines shown in the error and copy 'old' from them, or use edit_lines with those line numbers, or rewrite the whole file with write_file when most of it changes.
Use patch_many (or write_many) for several edits in one go. After a change that mattered, read the part you changed once to check it.
An identical call you already made returns the earlier result: use it and move on.
