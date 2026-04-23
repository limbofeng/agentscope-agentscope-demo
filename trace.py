import agentscope
import datetime
import shortuuid

#the project id should be replaced with a new solution
_projec_id="109"
_project_name="shengtai"
_initialized = False
_studio_url = "https://trace.agent.cstcloud.cn"


def init_trace():
    try:
        global _initialized
        if _initialized:
            print("Error: Project tracing is already initialized.")
            return
        if not _projec_id or not _project_name:
            print("Error: Project ID and name must be set before initializing trace.")
            return
        run_name=f"main_{_project_name}"
        run_id = f"{run_name}_{shortuuid.uuid()}"
        agentscope.init_main(project=_project_name,project_id=_projec_id,name=run_name,run_id=run_id,studio_url=_studio_url,global_trace_enabled=True)
        _initialized=True
    except Exception as e:
        print(f"Error initializing project tracing: {e}")


# connect the context with each query 
def attach_trace():
    try:
        if not _initialized:
            print("Error:Project tracing is not initialized. Call init_trace() first.")
            return
        # Register the run
        run_name=f"request_{datetime.datetime.now().isoformat()}"
        run_id = f"request_{shortuuid.uuid()}"
        agentscope.init_sub(project=_project_name,project_id=_projec_id,name=run_name,run_id=run_id,studio_url=_studio_url,global_trace_enabled=True)
    except Exception as e:
        print(f"Error attaching trace: {e}")