# NYC Smooth Streets Map

[Live map here](https://bob-foss.github.io/NYC-Street-Smoothness-Map/)

An interactive map of every street segment in New York City's five boroughs, colored by pavement quality. A slider sets the lowest rating you will accept, and every segment rated at or above it is highlighted. The project started as a tool for rollerbladers and skateboarders, whose small wheels make rough pavement matter far more than it does for cars or bikes.

The data refreshes itself from NYC Open Data once a month.

## What the map shows

- Segments rated at or above the slider value are drawn in green; the rest are drawn in faint gray.
- A histogram under the slider shows how many segments fall at each rating.
- A borough table shows the matching segment count and street miles.
- Hovering over or tapping a street shows its name, cross streets, borough, rating, and length.

## How the monthly refresh works

```
NYC Open Data (Street Pavement Ratings)
        |  1. GitHub Actions downloads every row through the dataset's API
        v
scripts/update_data.py
        |  2. keeps the newest inspection for each segment
        |  3. packs ratings, names, and simplified street lines
        v
data.json  --->  4. committed to this repo and published with index.html on GitHub Pages
```

- `.github/workflows/update-data.yml` runs on the 1st of every month, and whenever you press **Run workflow** on the Actions tab.
- The script refuses to publish a file with fewer than 50,000 segments, or one that drops more than 10% from the previous month, so a bad download cannot blank the live map.
- A push to `main` (for example, an edit to `index.html`) republishes the site with the existing `data.json` and does not download anything.

## Data

- **Source:** [Street Pavement Ratings](https://data.cityofnewyork.us/Transportation/Street-Pavement-Ratings/6yyb-pb25), NYC Open Data, published by the NYC Department of Transportation.
- **Ratings:** the newest inspection for each segment, on a scale of 1 to 10 where 10 is the smoothest. Some segments were last inspected years ago; the map shows the date range.
- **Rating 0:** about a quarter of segments carry a 0, which most likely means the segment has no rating. These are hidden unless the viewer chooses to include them.
- **Lines:** each segment's own geometry from the dataset, simplified to within about 5 feet.

## Run it yourself

```bash
python scripts/update_data.py data.json                        # download and rebuild
python scripts/update_data.py data.json --from-csv export.csv  # rebuild from a CSV you downloaded
python -m http.server 8000                                     # then open http://localhost:8000
```

The page loads `data.json` with `fetch`, so it needs a web server; opening `index.html` directly from disk shows a reminder instead of the map.

## Limitations

- Pavement ratings describe the roadway surface, not bike lanes, traffic, or other conditions that matter to skaters.
- Ratings are only as current as the city's inspections.
