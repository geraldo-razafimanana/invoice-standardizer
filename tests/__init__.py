import os

# Tests must not depend on the user's own_company.txt.
os.environ.setdefault("INVOICE_OWN_COMPANY_FILE", os.devnull)
