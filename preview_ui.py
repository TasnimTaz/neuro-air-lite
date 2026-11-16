"""Standalone runner for previewing Day 7 UI with a complete 6-Agent pipeline.
Does not touch Ollama or Postgres, perfect for instant UI testing of all agents.
"""
import asyncio
from api.app import create_app
from core.runtime import PythonRuntime
from domains.hk.viz import EChartsConfigManager

# Sequence covering all 6 agents with rich narrative analysis
RESPONSES = [
    # 1. Coordinator: Planning
    '[{"role": "knowledge_retrieval", "task": "Query WHO and HK AQO guideline thresholds"}, '
    '{"role": "data_acquisition", "task": "Fetch Tai Po fire monitoring sensor readings and OSM receptors"}, '
    '{"role": "multivariate_meteo", "task": "Join meteorological wind trajectory and plume dispersion"}, '
    '{"role": "pollution_analysis", "task": "Calculate peak concentration, z-score and pre-fire baseline ratio"}, '
    '{"role": "synthesis_report", "task": "Synthesize comprehensive atmospheric environmental report and generate timeline chart"}]',
    
    # 2. Knowledge Retrieval Worker
    "```python\n# Retrieve guideline standards\nwho_24h_pm25 = 15.0\nhk_aqo_pm25 = 50.0\nprint('Retrieved WHO threshold: 15 ug/m3, HK AQO: 50 ug/m3')\n```",
    "Retrieved official health benchmark thresholds: WHO 24h limit is 15 ug/m3 and HKEPD AQO limit is 50 ug/m3.",
    
    # 3. Data Acquisition Worker
    "```python\n# Data acquisition: Tai Po hourly records & OSM sensitive receptors\nstation = 'Tai Po'\nhourly_pm25 = [22.1, 24.5, 27.3, 28.0, 41.2, 49.5, 54.6, 38.2, 30.1, 25.4]\nreceptors = ['Alice Ho Miu Ling Nethersole Hospital', 'Tai Po Baptist Public School']\nprint(f'Fetched {len(hourly_pm25)} hourly readings and {len(receptors)} receptors within 2km.')\n```",
    "Loaded 10 chronological monitoring intervals for Tai Po station and identified 2 nearby sensitive receptors.",
    
    # 4. Multivariate Meteo Worker
    "```python\n# Meteo join: wind speed & direction\nwind_dir = 'North-East to South-West (NE-SW)'\nwind_speed = 14.2  # km/h\nprint(f'Plume trajectory: {wind_dir} at {wind_speed} km/h toward hospital corridor.')\n```",
    "Meteorological analysis confirms surface dispersion directed along a NE-SW corridor at 14.2 km/h.",
    
    # 5. Pollution Analysis Worker
    "```python\n# Pollution statistics\npre_fire_mean = 27.3\npeak_pm25 = max(hourly_pm25)\npeak_ratio = round(peak_pm25 / pre_fire_mean, 2)\nz_score = 2.45\nprint(f'Pre-fire: {pre_fire_mean}, Peak: {peak_pm25}, Ratio: {peak_ratio}x, Z: {z_score}')\n```",
    "Computed peak PM2.5 of 54.6 ug/m3 representing a 2.0x surge over the pre-fire baseline with z-score = 2.45.",
    
    # 6. Synthesis Report Worker: Comprehensive narrative breakdown
    "```python\n# Synthesis and Chart generation\nANSWER = {'Tai Po Peak PM2.5': peak_pm25, 'Baseline Mean': pre_fire_mean, 'Spike Ratio': f'{peak_ratio}x', 'Plume Trajectory': 'NE-SW (14.2 km/h)'}\n"
    "time_labels = ['T-12h', 'T-6h', 'Ignition', 'T+3h', 'Peak (+6h)', 'T+12h', 'T+18h', 'Post-24h']\n"
    "charts.line('Tai Po Incident Atmospheric Trajectory', time_labels, {'PM2.5 (ug/m3)': hourly_pm25[2:]}, 'ug/m3')\n"
    "print('ANSWER =', ANSWER)\n```",
    "### Executive Summary\n"
    "During the Wang Fuk Court fire incident at Tai Po, local atmospheric monitoring recorded an acute PM2.5 spike peaking at **54.6 µg/m³** within 6 hours of ignition. This corresponds to an **exact 2.0x surge (+100%)** over the 14-day pre-incident baseline mean of 27.3 µg/m³ (z-score = 2.45).\n\n"
    "### Atmospheric & Meteorological Dynamics\n"
    "Surface meteorological correlation reveals that a steady 14.2 km/h wind blowing from the North-East channeled the combustion plume directly along a South-West corridor. Rather than dispersing vertically, localized thermal inversion trapped particulate matter close to ground level during the peak afternoon hours.\n\n"
    "### Receptor Impact & Sensitive Populations\n"
    "Spatial distance analysis (PostGIS `ST_DWithin`) confirms that two high-priority receptor facilities fell directly within the 1.5 km downwind dispersion path:\n"
    "- **Alice Ho Miu Ling Nethersole Hospital** (1.4 km SW): Elevated exposure window lasted approximately 5 hours.\n"
    "- **Tai Po Baptist Public School** (850 m SW): Air quality index crossed into the *Very High* health risk band during dismissal hours.\n\n"
    "### Health Benchmark Compliance\n"
    "The 24-hour moving average concentration crossed the **WHO Air Quality Guideline threshold (15 µg/m³)** by a factor of 3.64x, while remaining just below the statutory Hong Kong AQO ceiling of 50 µg/m³.",
    
    # 7. Independent Verifier (single response completing verification)
    "```python\n# Independent SQL Re-computation\nprint('VERIFY: Fresh database query confirmed peak_pm25 == 54.6 ug/m3. Zero discrepancy.')\n```"
]

call_idx = 0

async def mock_llm(messages):
    global call_idx
    await asyncio.sleep(0.9)
    if call_idx < len(RESPONSES):
        resp = RESPONSES[call_idx]
        call_idx += 1
        return resp
    return "Orchestration complete."

def mock_runtime_factory():
    global call_idx
    call_idx = 0
    rt = PythonRuntime(variables={"charts": EChartsConfigManager()})
    return rt

app = create_app(
    runtime_factory=mock_runtime_factory,
    llm_func=mock_llm,
    timeout_seconds=90.0
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
