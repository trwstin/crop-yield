import pandas as pd

df = pd.read_excel('weather.xlsx')

def extract_year(date_str):
    parts = date_str.split('/')
    year = parts[-1]
    if len(year) == 2:  # If the year is in two-digit format
        year = '20' + year  # Assuming all years are in the 2000s
    return int(year)

def classify_season(date_str):
    month = int(date_str.split('/')[1])  # Extracting the month
    if month in [12, 1, 2]:
        return 'Winter'
    elif month in range(3, 10):
        return 'Summer'
    else:  # Covering months October and November
        return 'Autumn'
    

df['year'] = df['time'].apply(extract_year)
df['season'] = df['time'].apply(classify_season)

# Grouping by state, year, and season and aggregating the specified fields to get the average values

aggregated_df = df['year'] = df['time'].apply(extract_year).groupby(['state', 'year', 'season']).agg({
    'temperature_2m_min (°C)': 'mean',
    'temperature_2m_max (°C)': 'mean',
    'precipitation_sum (mm)': 'mean',
    'rain_sum (mm)': 'mean',
    'snowfall_sum (cm)': 'mean'
}).reset_index()

# Replacing all NaN values in the aggregated dataset with 0
aggregated_df = aggregated_df.fillna(0)
aggregated_df.to_excel('forecast_weather.xlsx', index = False)