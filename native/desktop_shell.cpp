// DesktopPilot's supported Shell COM bridge. No filesystem mutation APIs.
// All exported coordinates use the desktop view's client coordinate space.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <shlobj.h>
#include <shobjidl.h>
#include <shlguid.h>
#include <exdisp.h>
#include <servprov.h>
#include <intshcut.h>
#include <shellscalingapi.h>
#include <wrl/client.h>
#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <cwctype>
#include <cstring>
#include <iomanip>
#include <memory>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

using Microsoft::WRL::ComPtr;

namespace {
std::string utf8(const std::wstring& value) {
    if (value.empty()) return {};
    const int count = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.data(),
        static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr);
    if (!count) throw std::runtime_error("Invalid UTF-16 returned by Windows Shell.");
    std::string result(count, '\0');
    if (!WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.data(),
        static_cast<int>(value.size()), result.data(), count, nullptr, nullptr))
        throw std::runtime_error("Unable to encode Windows Shell metadata.");
    return result;
}

std::string quoted(const std::wstring& value) {
    std::ostringstream stream;
    stream << '"';
    for (const unsigned char ch : utf8(value)) {
        if (ch == '"') stream << "\\\"";
        else if (ch == '\\') stream << "\\\\";
        else if (ch < 0x20)
            stream << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                   << static_cast<unsigned>(ch) << std::dec;
        else stream << static_cast<char>(ch);
    }
    stream << '"';
    return stream.str();
}

void check(HRESULT hr, const char* operation) {
    if (FAILED(hr)) {
        std::ostringstream message;
        message << operation << " failed (HRESULT 0x" << std::hex
                << static_cast<unsigned long>(hr) << ").";
        throw std::runtime_error(message.str());
    }
}

class Apartment {
    HRESULT result_;
public:
    Apartment() : result_(CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED)) {
        check(result_, "COM STA initialization");
    }
    ~Apartment() { if (SUCCEEDED(result_)) CoUninitialize(); }
};

struct TaskFree {
    void operator()(ITEMIDLIST* value) const { CoTaskMemFree(value); }
};
using Pidl = std::unique_ptr<ITEMIDLIST, TaskFree>;

struct Item {
    Pidl pidl;
    std::wstring identity, name, parsing_name, target, arguments;
    POINT position{};
    bool folder = false, shortcut = false, filesystem = false;
};
struct Display {
    std::wstring id;
    RECT area{};
    UINT dpi = 96;
};

std::wstring display_name(IShellItem* item, SIGDN kind, bool required) {
    PWSTR text = nullptr;
    HRESULT result = item->GetDisplayName(kind, &text);
    if (FAILED(result)) {
        if (required) check(result, "IShellItem::GetDisplayName");
        return {};
    }
    const std::wstring value = text ? text : L"";
    CoTaskMemFree(text);
    if (required && value.empty()) throw std::runtime_error("Empty Shell item identity/name.");
    return value;
}

std::wstring file_identity(const std::wstring& path) {
    HANDLE handle = CreateFileW(path.c_str(), FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr, OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
    if (handle == INVALID_HANDLE_VALUE)
        throw std::runtime_error("Cannot obtain stable filesystem identity (Win32 " +
                                 std::to_string(GetLastError()) + ").");
    BY_HANDLE_FILE_INFORMATION info{};
    const BOOL result = GetFileInformationByHandle(handle, &info);
    const DWORD error = GetLastError();
    CloseHandle(handle);
    if (!result || (!info.nFileIndexHigh && !info.nFileIndexLow))
        throw std::runtime_error("Filesystem does not provide a stable file ID (Win32 " +
                                 std::to_string(error) + ").");
    std::wostringstream stream;
    stream << L"file:" << std::hex << info.dwVolumeSerialNumber << L":"
           << info.nFileIndexHigh << L":" << info.nFileIndexLow << L":"
           << info.ftCreationTime.dwHighDateTime << L":" << info.ftCreationTime.dwLowDateTime;
    return stream.str();
}

void shortcut_target(Item& item, const std::wstring& path) {
    if (!item.shortcut || path.empty()) return;
    const size_t index = path.find_last_of(L'.');
    std::wstring extension = index == std::wstring::npos ? L"" : path.substr(index);
    std::transform(extension.begin(), extension.end(), extension.begin(), towlower);
    if (extension == L".url") {
        ComPtr<IUniformResourceLocatorW> link;
        if (FAILED(CoCreateInstance(CLSID_InternetShortcut, nullptr, CLSCTX_INPROC_SERVER,
                                    IID_PPV_ARGS(&link)))) return;
        ComPtr<IPersistFile> persist;
        if (FAILED(link.As(&persist)) || FAILED(persist->Load(path.c_str(), STGM_READ))) return;
        PWSTR url = nullptr;
        if (SUCCEEDED(link->GetURL(&url)) && url) item.target = url;
        CoTaskMemFree(url);
    } else if (extension == L".lnk") {
        ComPtr<IShellLinkW> link;
        if (FAILED(CoCreateInstance(CLSID_ShellLink, nullptr, CLSCTX_INPROC_SERVER,
                                    IID_PPV_ARGS(&link)))) return;
        ComPtr<IPersistFile> persist;
        if (FAILED(link.As(&persist)) || FAILED(persist->Load(path.c_str(), STGM_READ))) return;
        std::vector<wchar_t> buffer(32768);
        // SLGP_RAWPATH only reads metadata; never Resolve(), activate or launch.
        if (SUCCEEDED(link->GetPath(buffer.data(), static_cast<int>(buffer.size()),
                                    nullptr, SLGP_RAWPATH))) item.target = buffer.data();
        buffer[0] = 0;
        if (SUCCEEDED(link->GetArguments(buffer.data(), static_cast<int>(buffer.size()))))
            item.arguments = buffer.data();
    }
}

BOOL CALLBACK find_list_view(HWND window, LPARAM parameter) {
    wchar_t name[128]{};
    GetClassNameW(window, name, 128);
    if (wcscmp(name, L"SysListView32") == 0) {
        *reinterpret_cast<HWND*>(parameter) = window;
        return FALSE;
    }
    return TRUE;
}

class DpiContext {
    DPI_AWARENESS_CONTEXT previous_ = nullptr;
public:
    explicit DpiContext(HWND window) {
        const auto context = GetWindowDpiAwarenessContext(window);
        if (!context) throw std::runtime_error("Cannot query Explorer DPI context.");
        previous_ = SetThreadDpiAwarenessContext(context);
        if (!previous_) throw std::runtime_error("Cannot match Explorer DPI coordinate context.");
    }
    ~DpiContext() { if (previous_) SetThreadDpiAwarenessContext(previous_); }
};

struct MonitorCollection {
    HWND coordinate_window;
    std::vector<Display> displays;
    std::string error;
};

BOOL CALLBACK collect_monitor(HMONITOR monitor, HDC, LPRECT, LPARAM parameter) {
    auto& collection = *reinterpret_cast<MonitorCollection*>(parameter);
    MONITORINFOEXW info{};
    info.cbSize = sizeof(info);
    if (!GetMonitorInfoW(monitor, &info)) {
        collection.error = "GetMonitorInfoW failed.";
        return FALSE;
    }
    POINT corners[2]{{info.rcWork.left, info.rcWork.top}, {info.rcWork.right, info.rcWork.bottom}};
    SetLastError(ERROR_SUCCESS);
    if (!MapWindowPoints(HWND_DESKTOP, collection.coordinate_window, corners, 2) &&
        GetLastError() != ERROR_SUCCESS) {
        collection.error = "Cannot map monitor work area to the desktop view.";
        return FALSE;
    }
    RECT area{std::min(corners[0].x, corners[1].x), std::min(corners[0].y, corners[1].y),
              std::max(corners[0].x, corners[1].x), std::max(corners[0].y, corners[1].y)};
    DEVICE_SCALE_FACTOR scale = SCALE_100_PERCENT;
    if (FAILED(GetScaleFactorForMonitor(monitor, &scale))) {
        collection.error = "Cannot query monitor DPI scale.";
        return FALSE;
    }
    // This is monitor metadata only. Points/spacing stay in Explorer's space.
    const UINT dpi = 96U * static_cast<UINT>(scale) / 100U;
    collection.displays.push_back({info.szDevice, area, dpi});
    return TRUE;
}

class Desktop {
    Apartment apartment_;
    ComPtr<IFolderView> view_;
    ComPtr<IShellFolder> folder_;
    HWND window_ = nullptr;
    std::unique_ptr<DpiContext> dpi_;
public:
    POINT spacing{};
    int icon_size = 32;
    bool auto_arrange = false;
    std::vector<Item> items;
    std::vector<Display> monitors;

    Desktop() {
        ComPtr<IShellWindows> windows;
        check(CoCreateInstance(CLSID_ShellWindows, nullptr, CLSCTX_ALL,
                                IID_PPV_ARGS(&windows)), "Create ShellWindows");
        VARIANT empty{};
        VariantInit(&empty);
        long unused_hwnd = 0;  // The legacy API's long HWND must not be used on x64.
        ComPtr<IDispatch> dispatch;
        check(windows->FindWindowSW(&empty, &empty, SWC_DESKTOP, &unused_hwnd,
                                    SWFO_NEEDDISPATCH, &dispatch), "Find desktop Shell window");
        if (!dispatch) throw std::runtime_error("Explorer has no interactive desktop Shell view.");
        ComPtr<IServiceProvider> services;
        check(dispatch.As(&services), "Desktop IServiceProvider");
        ComPtr<IShellBrowser> browser;
        check(services->QueryService(SID_STopLevelBrowser, IID_PPV_ARGS(&browser)),
              "Desktop IShellBrowser");
        ComPtr<IShellView> shell_view;
        check(browser->QueryActiveShellView(&shell_view), "Query desktop Shell view");
        check(shell_view.As(&view_), "Desktop IFolderView");
        check(view_->GetFolder(IID_PPV_ARGS(&folder_)), "Desktop Shell folder");
        HWND shell_window = nullptr;
        check(shell_view->GetWindow(&shell_window), "Desktop coordinate window");
        // Window discovery is for geometry only. No LVM_* positioning messages.
        find_list_view(shell_window, reinterpret_cast<LPARAM>(&window_));
        if (!window_) EnumChildWindows(shell_window, find_list_view, reinterpret_cast<LPARAM>(&window_));
        if (!window_) throw std::runtime_error("Desktop view coordinate window is unavailable.");
        dpi_ = std::make_unique<DpiContext>(window_);
        const HRESULT arrange = view_->GetAutoArrange();
        if (arrange != S_OK && arrange != S_FALSE) check(arrange, "IFolderView::GetAutoArrange");
        if (arrange != S_OK && arrange != S_FALSE)
            throw std::runtime_error("Unknown desktop auto-arrange state.");
        auto_arrange = arrange == S_OK;
        check(view_->GetSpacing(&spacing), "IFolderView::GetSpacing");
        if (spacing.x <= 0 || spacing.y <= 0)
            throw std::runtime_error("Explorer returned invalid desktop icon spacing.");
        ComPtr<IFolderView2> view2;
        if (SUCCEEDED(view_.As(&view2))) {
            FOLDERVIEWMODE mode{};
            check(view2->GetViewModeAndIconSize(&mode, &icon_size), "Desktop icon size");
        }
        MonitorCollection collection{window_, {}, {}};
        if (!EnumDisplayMonitors(nullptr, nullptr, collect_monitor,
                                  reinterpret_cast<LPARAM>(&collection)) || !collection.error.empty())
            throw std::runtime_error(collection.error.empty() ? "Cannot enumerate monitors." : collection.error);
        monitors = std::move(collection.displays);
        if (monitors.empty()) throw std::runtime_error("No usable desktop monitors.");
        std::sort(monitors.begin(), monitors.end(), [](const Display& a, const Display& b) {
            if (a.area.left != b.area.left) return a.area.left < b.area.left;
            return a.area.top < b.area.top;
        });
        enumerate();
    }

    void enumerate() {
        items.clear();
        ComPtr<IEnumIDList> enumeration;
        check(view_->Items(SVGIO_ALLVIEW, IID_PPV_ARGS(&enumeration)), "IFolderView::Items");
        std::set<std::wstring> identities;
        for (;;) {
            PITEMID_CHILD raw = nullptr;
            ULONG fetched = 0;
            const HRESULT next = enumeration->Next(1, &raw, &fetched);
            Pidl pidl(raw);
            if (next == S_FALSE) break;
            check(next, "Enumerate desktop Shell items");
            if (next != S_OK || fetched != 1 || !pidl)
                throw std::runtime_error("Incomplete desktop Shell enumeration.");
            Item item;
            item.pidl = std::move(pidl);
            check(view_->GetItemPosition(item.pidl.get(), &item.position), "IFolderView::GetItemPosition");
            ComPtr<IShellItem> shell_item;
            check(SHCreateItemWithParent(nullptr, folder_.Get(), item.pidl.get(),
                                         IID_PPV_ARGS(&shell_item)), "Create desktop Shell item");
            item.name = display_name(shell_item.Get(), SIGDN_NORMALDISPLAY, true);
            item.parsing_name = display_name(shell_item.Get(), SIGDN_DESKTOPABSOLUTEPARSING, true);
            SFGAOF attributes{};
            check(shell_item->GetAttributes(SFGAO_FOLDER | SFGAO_LINK | SFGAO_FILESYSTEM,
                                             &attributes), "Read desktop Shell item type");
            item.folder = (attributes & SFGAO_FOLDER) != 0;
            item.filesystem = (attributes & SFGAO_FILESYSTEM) != 0;
            const auto path = display_name(shell_item.Get(), SIGDN_FILESYSPATH, false);
            if (item.filesystem) {
                const DWORD file_attributes = GetFileAttributesW(path.c_str());
                if (file_attributes == INVALID_FILE_ATTRIBUTES)
                    throw std::runtime_error("Cannot read filesystem icon attributes.");
                // SFGAO_FOLDER also identifies browsable ZIP namespace items.
                item.folder = (file_attributes & FILE_ATTRIBUTE_DIRECTORY) != 0;
            }
            std::wstring extension;
            const auto dot = path.find_last_of(L'.');
            if (dot != std::wstring::npos) extension = path.substr(dot);
            std::transform(extension.begin(), extension.end(), extension.begin(), towlower);
            item.shortcut = (attributes & SFGAO_LINK) != 0 || extension == L".lnk" || extension == L".url";
            item.identity = item.filesystem ? file_identity(path) : L"shell:" + item.parsing_name;
            if (!identities.insert(item.identity).second)
                throw std::runtime_error("Ambiguous stable Shell identities; layout was not changed.");
            shortcut_target(item, path);
            items.push_back(std::move(item));
        }
    }

    std::string json() const {
        std::ostringstream out;
        out << "{\"icons\":[";
        bool comma = false;
        for (const auto& item : items) {
            if (comma) out << ',';
            comma = true;
            out << "{\"identity\":" << quoted(item.identity) << ",\"name\":" << quoted(item.name)
                << ",\"parsing_name\":" << quoted(item.parsing_name)
                << ",\"position\":{\"x\":" << item.position.x << ",\"y\":" << item.position.y << '}'
                << ",\"is_folder\":" << (item.folder ? "true" : "false")
                << ",\"is_shortcut\":" << (item.shortcut ? "true" : "false")
                << ",\"is_filesystem\":" << (item.filesystem ? "true" : "false")
                << ",\"target\":" << quoted(item.target) << ",\"arguments\":" << quoted(item.arguments) << '}';
        }
        out << "],\"monitors\":[";
        comma = false;
        for (const auto& monitor : monitors) {
            if (comma) out << ',';
            comma = true;
            out << "{\"id\":" << quoted(monitor.id) << ",\"work_area\":{\"left\":" << monitor.area.left
                << ",\"top\":" << monitor.area.top << ",\"right\":" << monitor.area.right
                << ",\"bottom\":" << monitor.area.bottom << "},\"dpi\":" << monitor.dpi << '}';
        }
        out << "],\"spacing\":{\"x\":" << spacing.x << ",\"y\":" << spacing.y
            << "},\"icon_size\":" << icon_size << ",\"auto_arrange\":" << (auto_arrange ? "true" : "false") << '}';
        return out.str();
    }

    static bool overlap(POINT a, POINT b, POINT size) {
        return std::abs(static_cast<long long>(a.x) - b.x) < size.x &&
               std::abs(static_cast<long long>(a.y) - b.y) < size.y;
    }

    void position(const wchar_t* const* ids, const POINT* points, UINT count) {
        if (auto_arrange) throw std::runtime_error("AUTO_ARRANGE_ENABLED: Turn off desktop View > Auto arrange icons.");
        if (!count) return;
        if (!ids || !points || count > items.size()) throw std::runtime_error("Invalid desktop positioning request.");
        std::set<std::wstring> requested;
        std::vector<PCUITEMID_CHILD> pidls;
        for (UINT index = 0; index < count; ++index) {
            if (!ids[index] || !requested.insert(ids[index]).second)
                throw std::runtime_error("Duplicate or invalid Shell identity.");
            auto match = std::find_if(items.begin(), items.end(),
                [&](const Item& item) { return item.identity == ids[index]; });
            if (match == items.end()) throw std::runtime_error("A desktop icon changed identity; refresh the preview.");
            bool visible = false;
            for (const auto& monitor : monitors) {
                const auto& r = monitor.area;
                if (points[index].x >= r.left && points[index].y >= r.top &&
                    static_cast<long long>(points[index].x) + spacing.x <= r.right &&
                    static_cast<long long>(points[index].y) + spacing.y <= r.bottom) visible = true;
            }
            if (!visible) throw std::runtime_error("A requested icon position is outside the usable desktop.");
            for (UINT prior = 0; prior < index; ++prior)
                if (overlap(points[index], points[prior], spacing))
                    throw std::runtime_error("Requested icon positions overlap.");
            pidls.push_back(match->pidl.get());
        }
        for (const auto& item : items) {
            if (requested.count(item.identity)) continue;
            for (UINT index = 0; index < count; ++index)
                if (overlap(points[index], item.position, spacing))
                    throw std::runtime_error("A requested position overlaps an unchanged desktop icon.");
        }
        // This is the only mutation in the bridge: positions, not user files.
        std::vector<POINT> mutable_points(points, points + count);
        const HRESULT current_auto_arrange = view_->GetAutoArrange();
        if (current_auto_arrange == S_OK)
            throw std::runtime_error("AUTO_ARRANGE_ENABLED: Turn off desktop View > Auto arrange icons.");
        if (current_auto_arrange != S_FALSE)
            throw std::runtime_error("Cannot confirm that desktop auto-arrange is disabled.");
        check(view_->SelectAndPositionItems(count, pidls.data(), mutable_points.data(),
              SVSI_POSITIONITEM | SVSI_NOSTATECHANGE), "IFolderView::SelectAndPositionItems");
        enumerate();  // Caller must verify Explorer's actual resulting positions.
    }
};

char* allocated_json(const std::string& json) {
    auto* result = static_cast<char*>(CoTaskMemAlloc(json.size() + 1));
    if (!result) return nullptr;
    memcpy(result, json.c_str(), json.size() + 1);
    return result;
}

std::string error_json(const std::exception& error) {
    // HRESULT and native diagnostics above are ASCII, hence safe UTF-16 widening.
    std::string message = error.what();
    return "{\"ok\":false,\"error\":" + quoted(std::wstring(message.begin(), message.end())) + "}";
}
}  // namespace

extern "C" __declspec(dllexport) char* DP_ReadDesktop() noexcept {
    try {
        Desktop desktop;
        return allocated_json("{\"ok\":true,\"state\":" + desktop.json() + "}");
    } catch (const std::exception& error) {
        return allocated_json(error_json(error));
    } catch (...) {
        return allocated_json("{\"ok\":false,\"error\":\"Unexpected native Shell failure.\"}");
    }
}

extern "C" __declspec(dllexport) char* DP_Position(
    const wchar_t* const* identities, const POINT* positions, UINT count) noexcept {
    try {
        Desktop desktop;
        desktop.position(identities, positions, count);
        return allocated_json("{\"ok\":true,\"state\":" + desktop.json() + "}");
    } catch (const std::exception& error) {
        return allocated_json(error_json(error));
    } catch (...) {
        return allocated_json("{\"ok\":false,\"error\":\"Unexpected native positioning failure.\"}");
    }
}

extern "C" __declspec(dllexport) void DP_Free(void* value) noexcept {
    CoTaskMemFree(value);
}
