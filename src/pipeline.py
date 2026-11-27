import boto3
from botocore import UNSIGNED
from botocore.config import Config
import numpy as np
from pyproj import Transformer
import io
import pandas as pd
from pathlib import Path

s3 = boto3.client(
    "s3",
    region_name = "us-east-1",
    config = Config(signature_version = UNSIGNED)
)

bucket = "argoverse"
prefix = "datasets/av2/motion-forecasting/train/"

print("S3 client initialized")


def get_scenario_folders(s3, bucket, prefix, max_folders=100):
    folders = []
    continuation_token = None

    while len(folders) < max_folders:
        params = {
            "Bucket": bucket,
            "Prefix": prefix,
            "Delimiter": "/",
            "MaxKeys": min(1000, max_folders - len(folders))
        }

        if continuation_token:
            params["ContinuationToken"] = continuation_token

        response = s3.list_objects_v2(**params)

        folders.extend(response.get("CommonPrefixes", []))

        if not response.get("IsTruncated", False):
            break
        continuation_token = response.get("NextContinuationToken")

        if not continuation_token:
            break

    return folders

def convert_to_gps(x, y):
    origin_lat = 40.44177902989321
    origin_lon = -80.01294377242584

    to_utm = Transformer.from_crs(
        "EPSG:4326", "EPSG:32617", always_xy=True
    )
    to_gps = Transformer.from_crs(
        "EPSG:32617", "EPSG:4326", always_xy=True
    )

    origin_x, origin_y = to_utm.transform(
        origin_lon, origin_lat
    )

    longitude, latitude = to_gps.transform(
        origin_x + x,
        origin_y + y
    )
    return latitude, longitude





def filter_pittsburgh_scenarios(s3, bucket, folders):
    pittsburgh_scenarios = []

    for folder in folders:
        folder_path = folder["Prefix"]
        scenario_id = folder_path.rstrip("/").split("/")[-1]
        key = folder_path + f"scenario_{scenario_id}.parquet"

        obj = s3.get_object(Bucket=bucket, Key=key)

        df = pd.read_parquet(
            io.BytesIO(obj["Body"].read()),
            columns=["city"]
        )

        city = df["city"].iloc[0]

        if city == "pittsburgh":
            pittsburgh_scenarios.append({
                "scenario_id": scenario_id,
                "s3_key": key
            })

    return pittsburgh_scenarios



# Converting all Pittsburgh scenarios to GPS coordinates
def get_scenario_locations(s3, bucket, pittsburgh_scenarios):
    scenario_locations = []

    for scenario in pittsburgh_scenarios:
        obj = s3.get_object(
            Bucket=bucket,
            Key=scenario["s3_key"]
        )

        scenario_df = pd.read_parquet(
            io.BytesIO(obj["Body"].read()),
            columns=["object_type", "observed", "position_x", "position_y"]
        )

        vehicles = scenario_df[
            (scenario_df["object_type"] == "vehicle") &
            (scenario_df["observed"] == True)
        ]

        if vehicles.empty:
            continue

        x = vehicles["position_x"].median()
        y = vehicles["position_y"].median()

        lat, lon = convert_to_gps(x, y)

        scenario_locations.append({
            "scenario_id": scenario["scenario_id"],
            "latitude": lat,
            "longitude": lon
        })

    return scenario_locations

def upload_to_s3(local_file, s3_key):
    # Use the restricted IAM user profile
    session = boto3.Session(
        profile_name="pittsburgh-dev",
        region_name="us-east-2"
    )

    s3_upload = session.client("s3")

    
    s3_upload.upload_file(
        local_file,
        "pittsburgh-traffic-safety-data",
        s3_key
    )

    print("Uploaded to S3:", s3_key)



def main():
    print("Argoverse 2 pipeline")

    Path("outputs").mkdir(parents=True, exist_ok=True)

    folders = get_scenario_folders(
        s3, bucket, prefix, max_folders=100
    )
    print("Total scenarios checked:", len(folders))

    pittsburgh_scenarios = filter_pittsburgh_scenarios(
            s3, bucket, folders
        )

    print("Pittsburgh scenarios found:", len(pittsburgh_scenarios))

    pgh_df = pd.DataFrame(pittsburgh_scenarios)
    pgh_df.to_csv(
        "outputs/pittsburgh_scenarios.csv",
        index=False
        )

    scenario_locations = get_scenario_locations(
            s3, bucket, pittsburgh_scenarios
        )

    locations_df = pd.DataFrame(scenario_locations)
    locations_df.to_csv(
        "outputs/pittsburgh_scenario_locations.csv",
        index=False
        )

    print("Pittsburgh scenarios mapped:", len(locations_df))
    print("GPS coordinates saved successfully!")


    # Step 5: Upload GPS results to private AWS S3
    upload_to_s3(
        "outputs/pittsburgh_scenario_locations.csv",
        "processed/pittsburgh_scenario_locations.csv"
    )



if __name__ == "__main__":
    main()