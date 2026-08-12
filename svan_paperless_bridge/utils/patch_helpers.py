"""
Patch helper utilities.
Provides unified tracing functionality for patches.
"""

import frappe


def tracert(msg):
    """
    Unified tracing function for patches.
    Outputs to console, logs, and UI.
    
    Args:
        msg (str): The message to trace
    """
    print(msg)
    frappe.logger("svan_paperless_bridge.patch").info(msg)
    frappe.msgprint(msg)
