# Diamondback Brewing Co — Event Pricing Reference

> Source PDFs: `docs/Reservation Pricing - Locust Point.pdf` and `docs/Reservation Pricing - Timonium.pdf`

---

## General Rules

- **Event fee includes:** Designated space for **2 hours** + food, drink, and gratuity
- **Weeknight:** Tuesday – Thursday
- **Weekend:** Friday – Sunday
- **Monday:** Not listed; treat as custom/case-by-case
- **>60 guests OR 4+ hours:** Reduced/custom pricing — work with customer individually
- **Outside alcohol:** Not permitted (beer and non-alcoholic only)
- **Outside food:** Not permitted (dessert exceptions with coordinator approval)
- **Inclement weather:** Outdoor → indoor swap requires 24-hour notice

---

## Pricing Tables (Both Locations — Same Rates)

### 2-Hour Rates

| Guests     | Weeknight | Weekend | # of Pizzas |
|------------|-----------|---------|-------------|
| 10 – 20    | $500      | $600    | 8           |
| 21 – 30    | $750      | $1,000  | 12          |
| 31 – 40    | $1,050    | $1,400  | 16          |
| 41 – 50    | $1,350    | $1,800  | 20          |
| 51 – 60    | $1,650    | $2,200  | 24          |
| > 60       | Custom    | Custom  | —           |

### 4-Hour Rates

| Guests     | Weeknight | Weekend  |
|------------|-----------|----------|
| 10 – 20    | $900      | $1,000   |
| 21 – 30    | $1,400    | $1,650   |
| 31 – 40    | $2,000    | $2,350   |
| 41 – 50    | $2,600    | $3,050   |
| 51 – 60    | $3,200    | $3,750   |
| > 60       | Custom    | Custom   |

### Pricing Formula
- Weeknight 2hr = $15 × (median of guest bracket) × 2
- Weekend 2hr = $20 × (median of guest bracket) × 2
- Weeknight 4hr = (Weeknight 2hr × 2) − $100
- Weekend 4hr = Weeknight 4hr + (Weekend 2hr − Weeknight 2hr)

---

## Location-Specific Details

### Locust Point
- **Taproom capacity:** 75 (50 seats)
- **Patio capacity:** 250 (150 seats)
- **Food:** Pizza buffet (5 pies: 4 classic, 1 seasonal) for parties >30; made-to-order otherwise
- **No Aveley Farms restriction**

### Timonium
- **Taproom capacity:** 120 (70 seats)
- **Patio capacity:** 40 (20 seats)
- **Food:** Pizza service (no buffet mentioned); small plates/salad on request (not included in fee)
- **Aveley Farms restriction:** Parties >30 guests **cannot start before 4:00 PM** (shared space with Aveley Farms Coffee)

---

## Pricing Logic for CRM Auto-Calculation

```
guest_brackets = [(10,20), (21,30), (31,40), (41,50), (51,60)]
weeknight_2hr  = [500, 750, 1050, 1350, 1650]
weekend_2hr    = [600, 1000, 1400, 1800, 2200]
weeknight_4hr  = [900, 1400, 2000, 2600, 3200]
weekend_4hr    = [1000, 1650, 2350, 3050, 3750]

If attendance > 60 OR day == Monday: price = "Custom"
If Fri/Sat/Sun: use weekend rates
If Tue/Wed/Thu: use weeknight rates
```
