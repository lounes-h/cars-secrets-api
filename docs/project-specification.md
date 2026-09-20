# Car’s Secrets — Project Specification

## 1. Project Overview

**Car’s Secrets** is a vehicle-auction discovery and intelligence platform that aggregates active and upcoming vehicle listings from multiple auction sources such as **Copart, IAA, Manheim**, and potentially other auction marketplaces into one searchable interface.

The platform is designed to save buyers and dealers time by eliminating the need to search multiple auction websites separately. Users will also be able to search directly by **VIN** to quickly locate a specific vehicle across supported auction sources.

In addition to displaying auction listings, Car’s Secrets will provide decision-support information that helps users understand whether a vehicle may be a good purchase and how much they should consider paying.

The core question the product should help answer is:

> **“Is this vehicle worth buying, and how much should I pay for it?”**

Car’s Secrets does **not** conduct auctions or place bids. Users will be redirected to the original auction provider when they are ready to bid or purchase a vehicle.

---

## 2. Primary Goals

Car’s Secrets should provide users with:

- One unified search experience across multiple vehicle auctions.
- VIN lookup across supported auction sources.
- Normalized vehicle and auction information regardless of the original source.
- Fast discovery of newly listed and upcoming auction vehicles.
- Estimated final auction price.
- Estimated retail / resale market value.
- Auction fee calculations.
- Estimated transportation and acquisition costs.
- Estimated repair or reconditioning cost where sufficient information exists.
- Estimated total investment in the vehicle.
- Potential profit and profit margin.
- Suggested maximum bid.
- Comparable vehicles and market pricing.
- AI-generated vehicle summaries.
- AI-generated risk flags and opportunity insights.
- Saved vehicles.
- Saved searches.
- Alerts for newly listed vehicles and important listing changes.
- Direct links to the original auction listing.

The platform should prioritize **useful buying decisions**, rather than simply duplicating information that already exists on auction websites.

---

## 3. Target Users

The primary users are:

**Independent used-car dealers**  
Dealers looking for inventory across multiple auction platforms.

**Salvage/rebuilder dealers**  
Buyers looking specifically for repairable Copart and IAA vehicles.

**Wholesalers and vehicle flippers**  
Users searching for vehicles that can potentially be resold profitably.

**Exporters**  
Buyers looking for vehicles suitable for international resale.

**Professional auction buyers**  
Users regularly monitoring large numbers of auction listings.

The system may also be useful to individual buyers, but the primary product experience should be designed around professional and frequent buyers.

---

## 4. User Access Levels

Car’s Secrets will support four user levels.

| User Type | Purpose |
|---|---|
| **Guest** | Allow visitors to discover the platform before creating an account |
| **Registered** | Free account with basic personalization features |
| **Pro** | Paid plan with advanced vehicle intelligence |
| **Premium** | Highest tier with deeper analytics, higher limits, and advanced tools |

Exact limits should remain configurable rather than hardcoded.

A possible initial access model is:

| Feature | Guest | Registered | Pro | Premium |
|---|:---:|:---:|:---:|:---:|
| Browse auction listings | ✓ | ✓ | ✓ | ✓ |
| Basic search/filtering | ✓ | ✓ | ✓ | ✓ |
| VIN lookup | Limited | ✓ | ✓ | ✓ |
| Vehicle details | Limited | ✓ | ✓ | ✓ |
| Save vehicles | — | ✓ | ✓ | ✓ |
| Saved searches | — | Limited | ✓ | ✓ |
| Alerts | — | Limited | ✓ | ✓ |
| Auction fee estimate | Basic | Basic | ✓ | ✓ |
| Estimated final auction price | — | Limited | ✓ | ✓ |
| Market/resale value | — | Limited | ✓ | ✓ |
| AI vehicle summary | — | Limited | ✓ | ✓ |
| Risk/opportunity insights | — | — | ✓ | ✓ |
| Suggested maximum bid | — | — | ✓ | ✓ |
| Profit estimate | — | — | ✓ | ✓ |
| Comparable vehicles | — | Limited | ✓ | ✓ |
| Advanced analytics | — | — | Limited | ✓ |
| Higher usage limits | — | — | ✓ | ✓ |

The entitlement system should be flexible so features and limits can be changed without restructuring the application.

---

## 5. Auction Sources

The platform should be designed around a provider-independent model.

Initial target sources:

- Copart
- IAA
- Manheim

Possible future sources:

- OPENLANE / ADESA
- SmartAuction
- Auction Edge
- ACV
- EBlock
- Other wholesale and salvage auctions

Source-specific fields should be normalized before they are presented to users.

---

## 6. Vehicle Listing Preview

The listing preview should answer:

> **“Is this vehicle worth opening?”**

Each listing card should prioritize the information most useful for making a quick buying decision.

Suggested information:

```text
Vehicle photo

2022 Toyota Camry SE

Copart
Raleigh, NC
42 miles away

Current Bid              $7,800
Suggested Max Bid        $9,400

Estimated Market Value  $17,500
Estimated Total Cost    $11,600
Potential Profit         $5,900
Profit Margin               34%

48,200 miles
Front End Damage
Salvage Title

Run & Drive
Keys Available
Buy Now

Auction:
Tomorrow • 10:00 AM

Profit Score: 91/100
```

The listing card should be easy to scan quickly, especially when users are reviewing dozens or hundreds of vehicles.

---

## 7. Vehicle Details Page

The details page should answer:

> **“Should I bid on this vehicle?”**

The page should contain several major information areas.

### Vehicle identity

Year, make, model, trim, VIN, body type, engine, transmission, drivetrain, fuel type, color, and mileage.

### Auction information

Auction provider, lot/stock number, auction location, auction date/time, lane/item number where available, current bid, Buy Now price, reserve status where available, seller information, and listing status.

### Condition

Primary damage, secondary damage, title type, title state, keys availability, run-and-drive status, starts status, odometer status, and other source-specific condition information.

### Photos

A responsive vehicle image gallery showing all legally available listing photographs.

### Deal Analysis

This should be one of the most prominent sections of the page.

Example:

```text
Current Bid                    $7,800

Suggested Maximum Bid          $9,400

If purchased for $9,400:

Auction Fees                   $1,350
Transportation                   $420
Estimated Repairs              $2,100
Other Estimated Costs              $0
────────────────────────────────────
Estimated Total Cost          $13,270

Estimated Resale Value        $18,900
────────────────────────────────────
Potential Profit               $5,630
Profit Margin                   29.8%

Profit Score                   91/100
Confidence                       High
```

Users should clearly understand that these numbers are **estimates**, not guarantees.

---

## 8. VIN Lookup

Users should be able to enter a VIN directly and search for matching active listings across all supported auction sources.

VIN lookup should:

- Search across all supported auction providers.
- Return the current active listing or listings associated with the VIN.
- Clearly identify the auction source.
- Show the lot or stock number.
- Show the auction date, location, current bid, Buy Now price where available, mileage, title, damage, photos, and other available listing data.
- Link directly to the full Car’s Secrets vehicle detail page.
- Provide a direct link to the original auction listing.
- Respect feature and usage limits based on the user’s subscription level.

VIN lookup is intended primarily as a fast discovery tool for current and upcoming auction inventory. Historical auction data is not required for the initial version.

---

## 9. Search and Filtering

Users should eventually be able to search across all supported auctions simultaneously.

Important filters include auction source, year, make, model, trim, mileage, title status, damage type, auction location, distance, sale date, current bid, Buy Now price, estimated market value, estimated profit, profit margin, total acquisition cost, and profit score.

Useful sorting options include:

- Highest expected profit
- Highest profit margin
- Highest profit score
- Lowest current bid
- Lowest total acquisition cost
- Soonest auction
- Newest listings
- Closest vehicles
- Highest estimated market value

---

## 10. Saved Vehicles and Watchlists

Registered users should be able to save vehicles.

Saved vehicles may later track changes such as:

- Current bid changed
- Auction date changed
- Buy Now price changed
- Vehicle was sold
- Vehicle was removed
- Vehicle was relisted
- Auction is starting soon

The dashboard should provide a centralized view of all watched vehicles.

---

## 11. Saved Searches and Alerts

Users should be able to save commonly used search criteria.

Example:

```text
Toyota Camry
2021–2025
Under 70,000 miles
Within 300 miles
Maximum estimated total cost: $15,000
Minimum expected profit: $3,000
```

When a matching vehicle appears, Car’s Secrets can notify the user.

Future notification channels may include:

- In-app
- Email
- Push notifications
- SMS

Availability may depend on subscription level.

---

## 12. Vehicle Intelligence

Car’s Secrets should provide information beyond the auction source's raw listing.

Important calculated or enriched values include:

### Estimated Final Auction Price

Prediction of the likely final selling price.

### Estimated Market Value

Estimated retail or resale value based on available market information.

### Auction Fees

Estimated buyer fees based on the auction source and predicted purchase price.

### Transportation Cost

Estimated cost of transporting the vehicle from the auction location.

### Repair / Reconditioning Estimate

Estimated repair costs when sufficient condition information is available.

### Estimated Total Acquisition Cost

```text
Purchase price
+ auction fees
+ transportation
+ repairs
+ additional costs
= estimated total cost
```

### Potential Profit

```text
Estimated resale value
- estimated total cost
= estimated profit
```

### Suggested Maximum Bid

The maximum purchase price that still satisfies a target profit or margin.

---

## 13. AI Features

AI should enhance the decision-making experience rather than exist simply as a chatbot.

Possible AI-generated information:

- Vehicle summary
- Damage summary
- Important listing details
- Potential concerns
- Positive indicators
- Risk flags
- Questions the buyer should investigate
- Comparison with similar listings
- Explanation of why the vehicle received its profit score

Example:

> **AI Summary:**  
> This Camry appears attractive because of its relatively low mileage, strong resale market, and moderate front-end damage. The current bid remains below the estimated profitable purchase range. Main risks are possible cooling-system damage and the salvage title. Based on current estimates, the vehicle remains attractive below approximately $9,400.

AI-generated information should always be clearly labeled as an estimate or analysis.

---

## 14. Profit / Opportunity Scoring

Each vehicle may eventually receive a score designed to help users prioritize inventory.

Example:

```text
Profit Score: 91/100
```

The score could consider:

- Expected profit
- Profit margin
- Market demand
- Vehicle liquidity
- Estimated repair cost
- Damage severity
- Price confidence
- Mileage
- Distance / transportation cost
- Auction price
- Comparable vehicles
- Days-to-sale expectations

The scoring algorithm should remain configurable.

---

## 15. Dashboard

Registered users should eventually have a personalized dashboard containing:

- Saved vehicles
- Saved searches
- Upcoming watched auctions
- Recent alerts
- New matching vehicles
- Recommended vehicles
- Top opportunities
- Recently viewed vehicles

Pro and Premium users may receive more advanced intelligence such as personalized recommendations.

---

## 16. Original Auction Integration

Car’s Secrets does **not** conduct auctions.

There should be no native bidding during the initial product phase.

The workflow is:

```text
Discover vehicle
      ↓
Analyze vehicle
      ↓
Calculate costs/profit
      ↓
Determine maximum bid
      ↓
Open original auction
      ↓
Bid directly through Copart / IAA / Manheim
```

Vehicle pages should therefore have prominent actions such as:

- View on Copart
- View on IAA
- View on Manheim

---

## 17. SEO Strategy

SEO should be treated as a core technical requirement from the beginning.

Future publicly indexable pages may include:

```text
/auction/copart
/auction/iaa
/auction/manheim

/make/toyota
/make/honda

/model/toyota/camry
/model/ford/f-150

/location/north-carolina
/location/texas

/vehicle/copart/12345678
```

Pages should support server-side rendering or static rendering where appropriate.

SEO requirements include unique metadata, canonical URLs, semantic HTML, structured data where appropriate, Open Graph metadata, sitemap generation, robots configuration, breadcrumb navigation, descriptive URLs, image optimization, fast page rendering, and strong Core Web Vitals.

Sensitive or user-specific pages such as dashboards, account pages, saved searches, and billing pages should not be indexed.

---

## 18. MVP Scope

The first version should remain focused.

A reasonable MVP would contain:

- Active auction listings
- Unified search
- VIN lookup
- Basic filters
- Vehicle detail pages
- Auction photos
- Current bid / Buy Now
- Auction date and location
- Basic fee calculator
- Estimated market value
- Estimated potential profit
- Suggested maximum bid
- Saved vehicles
- Basic alerts
- User accounts
- One or two paid subscription tiers
- Links to original auction platforms

Advanced features can follow after actual users demonstrate demand.

---

## 19. Out of Scope for Initial Version

The initial version should **not** attempt to implement:

- Native auction bidding
- Vehicle purchasing
- Escrow
- Dealer financing
- Transportation marketplace
- Full historical auction archive
- Complex AI automation
- Dealer management system
- Inventory management
- CRM
- Native mobile applications

These can be evaluated later.

---

## 20. Product Positioning

Car’s Secrets should not position itself simply as:

> **“All auctions in one place.”**

That is useful but relatively easy for competitors to replicate.

The stronger positioning is:

> **“Find the vehicles worth buying.”**

Or more explicitly:

> **Car’s Secrets searches multiple vehicle auctions and helps buyers understand what a car is worth, what it will really cost, and how much they should bid.**

The long-term product advantage should come from **decision intelligence**, not merely auction aggregation.
