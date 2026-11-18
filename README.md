
# Pittsburgh Traffic Safety Analysis

A data engineering and geospatial analytics project focused on identifying and analyzing potential traffic safety risks in Pittsburgh using the Argoverse 2 Motion Forecasting Dataset.

## Project Overview

This project combines transportation safety analysis with vehicle and pedestrian trajectory data to support the study of high-risk intersections in Pittsburgh.

The data pipeline is designed to:

1. Access Argoverse 2 data stored in AWS S3.
2. Identify driving scenarios collected in Pittsburgh.
3. Extract vehicle trajectory information.
4. Convert local coordinates into GPS coordinates.
5. Export processed scenario information to CSV files for further analysis.


## Installation

This pipeline requires Python and the packages listed in `requirements.txt`.

Install the dependencies:

```bash
python -m pip install -r requirements.txt
```

## Usage

Run the pipeline from the project root directory:

```bash
python src/pipeline.py
```

The pipeline currently processes the first 100 training scenarios from the Argoverse 2 Motion Forecasting Dataset.

It identifies Pittsburgh scenarios and converts their representative vehicle coordinates into GPS latitude and longitude.

## Output Files

The pipeline generates two CSV files in the `outputs/` directory:

- `pittsburgh_scenarios.csv`: Scenario IDs and AWS S3 file paths for Pittsburgh scenarios.
- `pittsburgh_scenario_locations.csv`: Scenario IDs and representative GPS coordinates.

These coordinates represent the median observed vehicle positions within each scenario, not the exact centers of intersections.

## Current Limitations

- The pipeline currently processes 100 training scenarios.
- GPS coordinates represent approximate scenario locations.
- Intersection matching and automated safety metrics are not yet implemented.
```

