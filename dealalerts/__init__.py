"""
Shared building blocks for the cigar deal alerts.

    core.py         fetching, deal rules, sent history, email, command line
    bigcommerce.py  stores on BigCommerce (Cigar King, Cigars N Cigars, Corona)
    shopify.py      stores on Shopify (GT Cigars)

Smoke Inn, Perfect Cigar Blend and Cigora read their own pages in their
scripts and use core.py for everything else.

A store script is just its settings plus run(); see README.md.
"""
