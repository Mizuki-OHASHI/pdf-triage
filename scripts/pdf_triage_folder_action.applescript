on adding folder items to this_folder after receiving added_items
	repeat with added_item in added_items
		set p to POSIX path of added_item
		set triage_script to POSIX path of (path to home folder) & ".local/bin/pdf_triage.py"
		do shell script quoted form of triage_script & " " & quoted form of p
	end repeat
end adding folder items to
