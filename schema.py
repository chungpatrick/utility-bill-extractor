"""Data model the agent fills in for every utility bill PDF.

A bill has one or more service addresses ("sites"). A normal bill has one site; a
multi-site summary invoice (e.g. TXU with 14 ESI IDs) has one per property.
For each site we capture one total cost, the individual charge line items behind it
(water, sewer, trash, taxes...), and the metered usage with units.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class UtilityType(str, Enum):
    electric = "electric"
    natural_gas = "natural_gas"
    water = "water"
    sewer = "sewer"
    irrigation = "irrigation"
    propane = "propane"
    fuel_oil = "fuel_oil"
    steam = "steam"
    other = "other"


class Confidence(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class Usage(BaseModel):
    utility_type: UtilityType
    meter_number: str | None = Field(description="Meter number / ESI ID / premise ID, if printed")
    usage_quantity: float = Field(description="Consumption for the period as printed (not a meter reading)")
    usage_unit: str | None = Field(
        description="Unit exactly as printed next to this usage (kWh, therms, CCF, HCF, gallons, kgal, m3...). Null if no unit is printed for it."
    )
    guessed_unit: str | None = Field(
        description="ONLY when usage_unit is null: your best guess of the unit, from clues elsewhere on the bill (usage chart labels, rate tables such as 'per 1,000 gal', meter multipliers, messages) or, failing that, the usual unit for this service and provider. Null when usage_unit is printed."
    )
    guess_basis: str | None = Field(
        description="ONLY when guessed_unit is set: short reason, e.g. 'usage chart labelled gallons' or 'typical for Texas municipal water'"
    )


class ChargeCategory(str, Enum):
    electric = "electric"
    natural_gas = "natural_gas"
    water = "water"
    sewer = "sewer"
    trash = "trash"            # refuse, garbage, recycling, hazardous/e-waste collection
    stormwater = "stormwater"  # drainage
    irrigation = "irrigation"
    tax = "tax"                # sales/municipal/state taxes and franchise fees shown as tax
    other = "other"            # program fees, ambulance (e.g. CareFlite), donations, deposits


class Charge(BaseModel):
    category: ChargeCategory = Field(description="Service the charge belongs to (base/customer/usage/delivery charges go to their service); taxes -> tax")
    description: str = Field(description="Line item label as printed, e.g. 'SEWER BASE', 'Refuse', 'Sales Tax'")
    amount: float = Field(description="Amount for this period; credits are negative")


class Site(BaseModel):
    address_as_printed: str = Field(
        description="The service address exactly as printed on the bill (same words, abbreviations and capitalization), on one line; join printed lines with ', '"
    )
    street: str = Field(description="Street line of the service address incl. unit, Title Case, e.g. '125 Maple Ave Unit 3B'")
    city: str | None = Field(description="City of the service address ONLY if printed with it; otherwise null")
    state: str | None = Field(description="2-letter state of the service address ONLY if printed with it; otherwise null")
    zip: str | None = Field(description="5-digit ZIP of the service address ONLY if printed with it; otherwise null")
    service_period_start: str | None = Field(description="YYYY-MM-DD")
    service_period_end: str | None = Field(description="YYYY-MM-DD")
    total_cost: float | None = Field(
        description="Total NEW charges for this address for the period: every service, tax and fee, excluding previous balance, payments and prior-period late fees"
    )
    charges: list[Charge] = Field(
        description="Every current-period charge line item for this address as printed (water, sewer, trash, taxes, fees...). Exclude previous balance, payments and prior-period late fees. Should add up to total_cost. If the bill shows only one total for the address, give one item."
    )
    usage: list[Usage] = Field(
        description="One entry per metered quantity. Flat-fee services (trash, base charges) have no usage entry. Do not repeat the water quantity for sewer billed on water consumption."
    )


class BillExtraction(BaseModel):
    provider_name: str
    provider_city: str | None = Field(description="City of the utility provider's own address printed on the bill (remit-to or office)")
    provider_state: str | None = Field(description="2-letter state of the provider's own address")
    account_number: str | None
    bill_date: str | None = Field(description="Statement/bill date, YYYY-MM-DD")
    due_date: str | None = Field(description="Payment due date, YYYY-MM-DD")
    current_charges: float | None = Field(
        description="Total new charges on the whole bill for this period (all sites), excluding previous balance, payments and prior late fees"
    )
    total_amount_due: float | None = Field(description="Total amount due as printed (may include previous unpaid balance)")
    currency: str = Field(description="ISO currency code, e.g. USD")
    sites: list[Site] = Field(description="One entry per distinct service address on the bill")
    confidence: Confidence = Field(description="Your confidence that every field above was read correctly")
    notes: str | None = Field(
        description="Anything a reviewer should know: estimated reads, credits, final bills, partial periods, missing units, illegible text"
    )
