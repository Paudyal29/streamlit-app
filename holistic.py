import os
import requests
from math import radians, sin, cos, sqrt, atan2
from datetime import datetime, timedelta
import streamlit as st
from streamlit_folium import st_folium
import folium
from supabase import create_client, Client
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")  # use anon key in production
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Helpers
def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1/a))

def is_time_overlap(start1, end1, start2, end2):
    return start1 < end2 and end1 > start2

@st.cache_data(ttl=300)
def get_chargers():
    return supabase.table("chargers").select("*").execute().data

@st.cache_data(ttl=300)
def get_stations():
    return supabase.table("stations").select("*").execute().data

# Fetch bookings for charger to check availability
def get_bookings_for_charger(charger_id, date):
    return supabase.table("bookings").select("start_time, duration_hours").eq("charger_id", charger_id).eq("date", date).execute().data

# Filter available chargers by time overlap
def filter_available_chargers(chargers, date, start_time_str, duration_hours):
    preferred_start = datetime.strptime(start_time_str, "%H:%M")
    preferred_end = preferred_start + timedelta(hours=duration_hours)
    available = []
    for c in chargers:
        bookings = get_bookings_for_charger(c['id'], date)
        free = True
        for b in bookings:
            b_start = datetime.strptime(b['start_time'], "%H:%M:%S")
            b_end = b_start + timedelta(hours=b['duration_hours'])
            if is_time_overlap(preferred_start, preferred_end, b_start, b_end):
                free = False
                break
        if free:
            available.append(c)
    return available

# Range calculation via external API
def calculate_range(start, end, capacity):
    url = "https://szktcpulamtvqkgxaoda.supabase.co/functions/v1/calculate-route"
    headers = {
        'Authorization': 'Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InN6a3RjcHVsYW10dnFrZ3hhb2RhIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDI2NzI0MTgsImV4cCI6MjA1ODI0ODQxOH0.F6DN2ox5w50xosTPgUHGqAEO9PqBOyCMQs5__pETQK0',
        'Content-Type': 'application/json'
    }
    payload = {"start": start, "end": end, "remaining_capacity": capacity, "MASS":"1720","EFFI":"0.1012"}
    resp = requests.post(url, json=payload, headers=headers)
    resp.raise_for_status()
    return resp.json()

# Booking insert
def create_booking(user_id, station_id, charger_id, date, start_time, duration, energy, price):
    supabase.table("bookings").insert({
        "user_id": user_id,
        "station_id": station_id,
        "charger_id": charger_id,
        "date": date,
        "start_time": start_time,
        "duration_hours": duration,
        "energy_kwh": energy,
        "price": price,
        "status": "confirmed",
        "payment_status": "pending"
    }).execute()

# Draw route on folium map with colored segments
def draw_route(m, coords, green_coord, orange_coord, red_coord):
    latlngs = [(p['lat'], p['lon']) for p in coords]
    def find_index(target):
        if not target:
            return None
        for i, p in enumerate(coords):
            if abs(p['lat'] - target.get('lat', None)) < 1e-6 and abs(p['lon'] - target.get('lon', None)) < 1e-6:
                return i
        return None
    gi = find_index(green_coord)
    oi = find_index(orange_coord)
    ri = find_index(red_coord)
    if gi is None and green_coord:
        gi = int(len(latlngs)*0.3)
    if oi is None and orange_coord:
        oi = int(len(latlngs)*0.6)
    if ri is None and red_coord:
        ri = int(len(latlngs)*0.9)
    if gi is not None:
        folium.PolyLine(latlngs[:gi+1], color='green', weight=5, opacity=0.7).add_to(m)
    if oi is not None:
        start = gi or 0
        folium.PolyLine(latlngs[start:oi+1], color='orange', weight=5, opacity=0.7).add_to(m)
    start = oi or gi or 0
    folium.PolyLine(latlngs[start:], color='red', weight=5, opacity=0.7).add_to(m)

# Streamlit App
def main():
    st.set_page_config(page_title="EV Route & Charging Booking", layout="wide")
    st.title("🚗 EV Route & Charging Station Booking")

    # Authentication
    if "user" not in st.session_state:
        st.subheader("Login to continue")
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        if st.button("Login"):
            try:
                user_session = supabase.auth.sign_in_with_password({"email": email, "password": password})
                st.session_state.user = user_session.user
                st.success(f"Logged in as {email}")
            except Exception:
                st.error("Login failed.")
        return

    # Sidebar: Route inputs
    st.sidebar.header("Route & Range")
    with st.sidebar.form(key="route_form"):
        lat0 = st.number_input("Latitude", value=27.691610, format="%.6f", key="lat0")
        lon0 = st.number_input("Longitude", value=85.2743222, format="%.6f", key="lon0")
        lat1 = st.number_input("Dest Latitude", value=27.4376028, format="%.6f", key="lat1")
        lon1 = st.number_input("Dest Longitude", value=85.7874298, format="%.6f", key="lon1")
        capacity = st.number_input("Remaining Battery (kWh)", value=9.0, step=0.5, key="capacity")
        submit = st.form_submit_button("Compute Range")

    if submit:
        try:
            st.session_state.data = calculate_range({"lat": st.session_state.lat0, "lon": st.session_state.lon0},
                                                    {"lat": st.session_state.lat1, "lon": st.session_state.lon1},
                                                    st.session_state.capacity)
        except Exception as e:
            st.error(f"Range API error: {e}")

    # Display map & booking only after range calculated
    if "data" in st.session_state:
        d = st.session_state.data
        coords = d.get("route_coordinates") or []
        gz = (d.get("green_zone") or {}).get("coordinate")
        oz = (d.get("orange_zone") or {}).get("coordinate")
        rz = (d.get("red_zone") or {}).get("coordinate")

        st.subheader("Route & Nearby Stations Map")
        if coords:
            # initialize map at midpoint
            all_points = [(p['lat'], p['lon']) for p in coords]
            m = folium.Map(location=all_points[0], zoom_start=12)
            draw_route(m, coords, gz, oz, rz)

            # mark all stations and collect station points
            stations = get_stations()
            station_points = []
            for s in stations:
                point = (s['latitude'], s['longitude'])
                station_points.append(point)
                folium.Marker(point,
                              popup=f"Station {s['id']}: {s.get('location','')}" ,
                              icon=folium.Icon(color='blue', icon='charging-station', prefix='fa')
                ).add_to(m)

            # fit map to include route and stations
            m.fit_bounds(all_points + station_points)
            st_folium(m, width=700, height=500)
        else:
            st.warning("No route coordinates to display.")

        # Booking inputs: date/time before station selection
        st.subheader("Booking Details")
        date = st.date_input("Booking Date", key="b_date")
        start_time = st.text_input("Start Time (HH:MM)", "12:00", key="b_time")
        duration = st.number_input("Duration (hours)", min_value=0.5, step=0.5, key="b_duration")

        # Select station
        st.subheader("Select Station & Charger")
        nearby_stations = []
        if rz:
            for s in get_stations():
                if haversine(rz['lat'], rz['lon'], s['latitude'], s['longitude']) <= 200:
                    nearby_stations.append(s)
        if not nearby_stations:
            st.warning("No stations within 5 km of green-zone end.")
            return
        station_labels = [f"ID {s['id']} — {s.get('location','')}" for s in nearby_stations]
        st_choice = st.selectbox("Select Station", station_labels, key="station_choice")
        station = nearby_stations[station_labels.index(st_choice)]

        # Filter chargers at station by availability
        chargers = [c for c in get_chargers() if c['station_id']==station['id']]
        available = filter_available_chargers(chargers, str(date), start_time, duration)
        if not available:
            st.warning("No available chargers at this station & time.")
            return
        charger_labels = [f"ID {c['id']} — {c['charger_type']} ({c['power_output']} kW) @ ${c['price_per_kwh']}/kWh" for c in available]
        ch_choice = st.selectbox("Select Charger", charger_labels, key="charger_choice")
        charger = available[charger_labels.index(ch_choice)]

        # Energy input & final booking
        energy = st.number_input("Energy to draw (kWh)", min_value=0.5, step=0.5, key="b_energy")
        price = energy * charger['price_per_kwh']
        st.write(f"Estimated Price: ${price:.2f}")
        if st.button("Confirm Booking", key="confirm_booking"):
            create_booking(st.session_state.user.id, station['id'], charger['id'], str(date), start_time, duration, energy, price)
            st.success("Booking confirmed!")

if __name__ == "__main__":
    main()
